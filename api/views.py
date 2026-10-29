import hashlib
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.paginator import InvalidPage
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, serializers, status
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Document, QueryLog, UserSession


def hash_token(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def issue_user_session(user):
    expires_at = timezone.now() + timedelta(
        seconds=settings.TOKEN_SESSION_LIFETIME_SECONDS
    )
    for _ in range(5):
        raw_token = secrets.token_urlsafe(32)
        try:
            with transaction.atomic():
                session = UserSession.objects.create(
                    user=user,
                    token=hash_token(raw_token),
                    expires_at=expires_at,
                )
        except IntegrityError:
            continue
        return raw_token, session
    raise RuntimeError('Unable to allocate a unique token session.')


class ExpiringTokenAuthentication(BaseAuthentication):
    keyword = 'Token'

    def authenticate(self, request):
        authorization = get_authorization_header(request).split()
        if not authorization:
            return None
        if authorization[0].lower() != self.keyword.lower().encode('ascii'):
            raise AuthenticationFailed('Authorization header must use the Token scheme.')
        if len(authorization) != 2:
            raise AuthenticationFailed('Invalid token header.')

        try:
            raw_token = authorization[1].decode('utf-8')
        except UnicodeError:
            raise AuthenticationFailed('Invalid token header.')

        session = (
            UserSession.objects.select_related('user')
            .filter(token=hash_token(raw_token))
            .first()
        )
        if session is None:
            raise AuthenticationFailed('Invalid token.')
        if session.expires_at <= timezone.now():
            session.delete()
            raise AuthenticationFailed('Token has expired.')
        if not session.user.is_active:
            raise AuthenticationFailed('User account is inactive.')
        return session.user, session

    def authenticate_header(self, request):
        return self.keyword


User = get_user_model()


class UserIdentitySerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'email')
        read_only_fields = ('id', 'username', 'email')


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={'input_type': 'password'},
    )
    email = serializers.EmailField(required=False, allow_blank=True, max_length=254)

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password')
        read_only_fields = ('id',)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        candidate = User(
            username=attrs.get('username', ''),
            email=attrs.get('email', ''),
        )
        try:
            validate_password(attrs['password'], candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({'password': error.messages})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            password=validated_data['password'],
        )


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(
        trim_whitespace=False,
        style={'input_type': 'password'},
    )

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs.get('username'),
            password=attrs.get('password'),
        )
        if user is None or not user.is_active:
            raise serializers.ValidationError(
                {'non_field_errors': ['Unable to log in with the provided credentials.']}
            )
        attrs['user'] = user
        return attrs


class DocumentSerializer(serializers.ModelSerializer):
    owner = serializers.IntegerField(source='owner_id', read_only=True)
    content = serializers.CharField(
        required=True,
        allow_blank=True,
        trim_whitespace=False,
    )

    class Meta:
        model = Document
        fields = ('id', 'title', 'content', 'uploaded_at', 'owner')
        read_only_fields = ('id', 'uploaded_at', 'owner')


class QueryLogSerializer(serializers.ModelSerializer):
    user = serializers.IntegerField(source='user_id', read_only=True)

    class Meta:
        model = QueryLog
        fields = ('id', 'query_text', 'result_summary', 'created_at', 'user')
        read_only_fields = ('id', 'result_summary', 'created_at', 'user')


class StandardPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100

    def _positive_integer(self, raw_value, field_name, maximum=None):
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = None

        if value is None or value < 1 or (maximum is not None and value > maximum):
            if maximum is None:
                message = 'Must be a positive integer.'
            else:
                message = f'Must be an integer between 1 and {maximum}.'
            raise serializers.ValidationError({field_name: [message]})
        return value

    def get_page_size(self, request):
        raw_value = request.query_params.get(self.page_size_query_param)
        if raw_value is None:
            return self.page_size
        return self._positive_integer(
            raw_value,
            self.page_size_query_param,
            self.max_page_size,
        )

    def get_page_number(self, request, paginator):
        raw_value = request.query_params.get(self.page_query_param)
        if raw_value is None:
            return 1
        return self._positive_integer(raw_value, self.page_query_param)

    def paginate_queryset(self, queryset, request, view=None):
        page_size = self.get_page_size(request)
        paginator = self.django_paginator_class(queryset, page_size)
        page_number = self.get_page_number(request, paginator)
        try:
            self.page = paginator.page(page_number)
        except InvalidPage:
            raise serializers.ValidationError({'page': ['Page is out of range.']})

        if paginator.num_pages > 1 and self.template is not None:
            self.display_page_controls = True
        self.request = request
        return list(self.page)


QUERY_RESULT_SUMMARY = 'Query accepted; processing is not configured.'
ALLOWED_UPLOAD_MIME_TYPES = {
    '.txt': {'text/plain'},
    '.md': {
        'text/plain',
        'text/markdown',
        'text/x-markdown',
        'application/markdown',
    },
}


class UploadTooLarge(APIException):
    status_code = 413
    default_detail = 'Uploaded file exceeds the 5 MiB limit.'
    default_code = 'file_too_large'


def _document_data_from_upload(uploaded_file):
    filename = str(uploaded_file.name or '').replace('\\', '/').rsplit('/', 1)[-1]
    suffix = Path(filename).suffix.lower()
    accepted_mime_types = ALLOWED_UPLOAD_MIME_TYPES.get(suffix)
    if accepted_mime_types is None:
        raise serializers.ValidationError(
            {'file': ['Unsupported file extension. Only .txt and .md files are accepted.']}
        )

    declared_mime_type = (uploaded_file.content_type or '').split(';', 1)[0].strip().lower()
    if declared_mime_type not in accepted_mime_types:
        raise serializers.ValidationError(
            {'file': ['Unsupported MIME type for this file extension.']}
        )

    reported_size = getattr(uploaded_file, 'size', None)
    if reported_size is None:
        raise serializers.ValidationError({'file': ['Unable to determine upload size.']})
    if reported_size > settings.DOCUMENT_UPLOAD_MAX_BYTES:
        raise UploadTooLarge()

    raw_content = uploaded_file.read()
    if len(raw_content) > settings.DOCUMENT_UPLOAD_MAX_BYTES:
        raise UploadTooLarge()
    try:
        content = raw_content.decode('utf-8')
    except (AttributeError, UnicodeDecodeError):
        raise serializers.ValidationError({'file': ['File must be valid UTF-8 text.']})
    if '\x00' in content:
        raise serializers.ValidationError({'file': ['File must not contain NUL bytes.']})

    title = Path(filename).stem.strip() or 'Untitled document'
    return {'title': title, 'content': content}


class RegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                user = serializer.save()
                raw_token, session = issue_user_session(user)
        except IntegrityError:
            raise serializers.ValidationError(
                {'username': ['A user with that username already exists.']}
            )

        return Response(
            {
                'user': UserIdentitySerializer(user).data,
                'token': raw_token,
                'expires_at': session.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        raw_token, session = issue_user_session(user)
        return Response(
            {
                'user': UserIdentitySerializer(user).data,
                'token': raw_token,
                'expires_at': session.expires_at,
            },
            status=status.HTTP_200_OK,
        )


class DocumentCollectionView(generics.GenericAPIView):
    authentication_classes = [ExpiringTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = DocumentSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        return Document.objects.filter(owner=self.request.user)

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if uploaded_file is not None:
            serializer = self.get_serializer(data=_document_data_from_upload(uploaded_file))
        else:
            if request.FILES:
                raise serializers.ValidationError(
                    {'file': ['Use the multipart field named file.']}
                )
            media_type = request.META.get('CONTENT_TYPE', '').split(';', 1)[0].lower()
            if media_type != 'application/json':
                raise serializers.ValidationError(
                    {
                        'non_field_errors': [
                            'Submit JSON with title and content, or multipart data with a file field.'
                        ]
                    }
                )
            serializer = self.get_serializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        document = serializer.save(owner=request.user)
        return Response(
            self.get_serializer(document).data,
            status=status.HTTP_201_CREATED,
        )


class DocumentDetailView(APIView):
    authentication_classes = [ExpiringTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def _get_owned_document(self, request, pk):
        return get_object_or_404(Document, pk=pk, owner=request.user)

    def get(self, request, pk):
        document = self._get_owned_document(request, pk)
        return Response(DocumentSerializer(document).data)

    def _update(self, request, pk, partial):
        document = self._get_owned_document(request, pk)
        serializer = DocumentSerializer(document, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def delete(self, request, pk):
        document = get_object_or_404(Document, pk=pk)
        if document.owner_id != request.user.pk:
            raise PermissionDenied('You do not have permission to delete this document.')
        document.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class QueryCreateView(APIView):
    authentication_classes = [ExpiringTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = QueryLogSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query_log = serializer.save(
            user=request.user,
            result_summary=QUERY_RESULT_SUMMARY,
        )
        return Response(QueryLogSerializer(query_log).data, status=status.HTTP_201_CREATED)


class QueryListView(generics.GenericAPIView):
    authentication_classes = [ExpiringTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = QueryLogSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        return QueryLog.objects.filter(user=self.request.user)

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)
