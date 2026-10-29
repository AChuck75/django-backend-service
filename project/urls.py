from django.contrib import admin
from django.urls import path, re_path

from api.views import (
    DocumentCollectionView,
    DocumentDetailView,
    LoginView,
    QueryCreateView,
    QueryListView,
    RegistrationView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    re_path(r'^auth/register/?$', RegistrationView.as_view(), name='auth-register'),
    re_path(r'^auth/login/?$', LoginView.as_view(), name='auth-login'),
    re_path(r'^documents/?$', DocumentCollectionView.as_view(), name='document-list'),
    re_path(
        r'^documents/(?P<pk>[0-9]+)/?$',
        DocumentDetailView.as_view(),
        name='document-detail',
    ),
    re_path(r'^query/?$', QueryCreateView.as_view(), name='query-create'),
    re_path(r'^queries/?$', QueryListView.as_view(), name='query-list'),
]
