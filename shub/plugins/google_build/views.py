'''

Copyright (C) 2019 Vanessa Sochat.

This Source Code Form is subject to the terms of the
Mozilla Public License, v. 2.0. If a copy of the MPL was not distributed
with this file, You can obtain one at http://mozilla.org/MPL/2.0/.

'''

from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import (
    FormParser, 
    MultiPartParser
)

from django.contrib import messages
from django.shortcuts import (
    render, 
    redirect
)

from shub.apps.main.views import get_container
from shub.apps.main.models import (
    Collection, 
    Container
)
from rest_framework.viewsets import ModelViewSet
from .models import RecipeFile
from shub.apps.api.utils import (
    get_request_user,
    validate_request,
    has_permission
)
from rest_framework import serializers
from sregistry.main.registry.auth import generate_timestamp
from .github import (
    receive_github_hook,
    create_webhook,
    get_repo,
    list_repos
)

import django_rq
from datetime import (
    datetime, 
    timedelta
)

from .actions import (
    complete_build,
    delete_build
)

from .utils import JsonResponseMessage
import re
import ast
import json
import uuid

@login_required
def connect_github(request):
    '''create a new container collection based on connecting GitHub.
    '''

    # All repos owned by the user on GitHub are contenders
    contenders = list_repos(request.user)

    # Filter down to repos that haven't had an equivalent URI added
    # This is intentionally different from the uri that we push so that only
    # builds can be supported from GitHub (and they don't cross contaminate)
    collections = [x.name for x in Collection.objects.filter(owners=request.user)]

    # Only requirement is that URI (name) isn't already taken, add to repos
    repos = []
    for repo in contenders:
        if repo['full_name'] not in collections:
            repos.append(repo)

    context = {"repos": repos}
    return render(request, "google_build/add_collection.html", context)


@login_required
def save_collection(request):
    '''save the newly selected collection by the user.
    '''

    if request.method == "POST":

        # The checked repos are sent in format REPO_{{ repo.owner.login }}/{{ repo.name }}
        repos = [x.replace('REPO_','') for x in request.POST.keys() if re.search("^REPO_", x)] 
        secret = uuid.uuid4().__str__()

        if len(repos) > 0:

            # If the user doesn't have permission to create a collection
            if not request.user.has_create_permission():
                messages.error("You do not have permission to create a collection.")
                return redirect('collections')

            # Always just take the first one
            username, reponame = repos[0].split('/')

            # Retrieve the repo fully
            repo = get_repo(request.user,
                            reponame=reponame,
                            username=username)

            webhook = create_webhook(user=request.user,
                                     repo=repo,
                                     secret=secret)

            if "errors" in webhook:

                # If there is an error, we should tell user about it
                message = ','.join([x['message'] for x in webhook['errors']])
                messages.info(request,"Errors: %s" % message)

            # If the webhook was successful, it will have a ping_url
            elif "ping_url" in webhook:

                collection = Collection.objects.create(secret=secret, 
                                                       name=repo['full_name'])

                # Add minimal metadata about repo and webhook
                collection.metadata['github'] = {'webhook': webhook,
                                                 'repo': repo['clone_url'],
                                                 'created_at': repo['created_at'],
                                                 'updated_at': repo['updated_at'],
                                                 'pushed_at': repo['pushed_at'],
                                                 'repo_id': repo['id'],
                                                 'repo_name': repo['full_name']}

                collection.owners.add(request.user)                        

                # Add tags
                if "topics" in webhook:
                    if webhook['topics']:
                        for topic in webhook['topics']:
                            collection.tags.add(topic)
                        collection.save()

                collection.save() # probably not necessary
                return redirect(collection.get_absolute_url())

    return redirect('collections')


class RecipePushSerializer(serializers.HyperlinkedModelSerializer):

    class Meta:
        model = RecipeFile
        fields = ('created', 'datafile','collection','tag','name',)


class RecipePushViewSet(ModelViewSet):
    '''pushing a recipe coincides with doing a remote build.
    '''
    queryset = RecipeFile.objects.all()
    serializer_class = RecipePushSerializer
    parser_classes = (MultiPartParser, FormParser,)

    def perform_create(self, serializer):

        print(self.request.data) 
        tag = self.request.data.get('tag','latest')                                   
        name = self.request.data.get('name')
        auth = self.request.META.get('HTTP_AUTHORIZATION', None)
        collection_name = self.request.data.get('collection')

        # Authentication always required for push

        if auth is None:
            raise PermissionDenied(detail="Authentication Required")

        owner = get_request_user(auth)
        timestamp = generate_timestamp()
        payload = "build|%s|%s|%s|%s|" %(collection_name,
                                         timestamp,
                                         name,
                                         tag)


        # Validate Payload

        if not validate_request(auth, payload, "build", timestamp):
            raise PermissionDenied(detail="Unauthorized")

        # Does the user have create permission?
        if not owner.has_create_permission():
            raise PermissionDenied(detail="Unauthorized Create Permission")

        create_new = False

        # Determine the collection to build the recipe to
        try:
            collection = Collection.objects.get(name=collection_name)

            # Only owners can push to existing collections
            if not owner in collection.owners.all():
                raise PermissionDenied(detail="Unauthorized")

        except Collection.DoesNotExist:
            raise PermissionDenied(detail="Not Found")

        # Validate User Permissions
        if not has_permission(auth, collection, pull_permission=False):
            raise PermissionDenied(detail="Unauthorized")
        
        # The collection must exist when we get here
        try:
            container = Container.objects.get(collection=collection,
                                              name=name,
                                              tag=tag)
            if not container.frozen:
                create_new = True

        except Container.DoesNotExist:
            create_new=True
        
        # Create the recipe to trigger a build
 
        if create_new is True:
            serializer.save(datafile=self.request.data.get('datafile'),
                            collection=self.request.data.get('collection'),
                            tag=self.request.data.get('tag','latest'),
                            name=self.request.data.get('name'),
                            owner_id=owner.id)
        else:
            raise PermissionDenied(detail="%s is frozen, push not allowed." % container.get_short_uri())


# Receive GitHub Hook

@csrf_exempt
def receive_build(request, cid):
    '''receive_build will receive the post from Google Cloud Build.
       TODO: how else can we authenticate this?
    '''
    print(request.body)
    print(cid)

    if request.method == "POST":
        container = Container.objects.get(id=cid)
        params = ast.literal_eval(json.loads(request.body.decode('utf-8')))
        scheduler = django_rq.get_scheduler('default')

        # Content length is always 47
        if request.META['CONTENT_LENGTH'] == "47":
            job = scheduler.enqueue_in(timedelta(seconds=10),
                                       complete_build, 
                                       cid=container.id, 
                                       params=params)
        # TODO: can we limit to receiving from Google Build servers?

    return JsonResponseMessage(message="Notification Received",
                               status=200,
                               status_message="Received")

@login_required
def delete_container(request, cid):
    '''delete a container, including it's corresponding files
       that are stored in Google Build (if they exist)
    '''
    container = get_container(cid)

    if not container.has_edit_permission(request):
        messages.info(request,"This action is not permitted.")
        return redirect('collections')

    # Send a job to the worker to delete the build files
    django_rq.enqueue(delete_build, cid=container.id)
    container.delete()
    messages.info(request,'Container successfully deleted.')
    return redirect(container.collection.get_absolute_url())


@csrf_exempt
def receive_hook(request):
    '''receive_hook will forward a hook to the correct receiver depending on 
       the header information. If it cannot be determined, it is ignored.
    '''
    if request.method == "POST":

        # Has to have Github-Hookshot
        if re.search('GitHub-Hookshot', request.META["HTTP_USER_AGENT"]) is not None:
            return receive_github_hook(request)

    return JsonResponseMessage(message="Invalid request.")
