from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import Document, QueryLog, UserSession
from .views import QUERY_RESULT_SUMMARY, hash_token, issue_user_session

User = get_user_model()


class AuthenticationEndpointTests(APITestCase):
    def test_registration_creates_hashed_session_and_login_returns_new_token(self):
        registration = self.client.post(
            '/auth/register',
            {
                'username': 'alice',
                'password': 'BrightComet!4729',
                'email': 'alice@example.com',
            },
            format='json',
        )

        self.assertEqual(registration.status_code, status.HTTP_201_CREATED)
        self.assertEqual(registration.data['user']['username'], 'alice')
        self.assertNotIn('password', registration.data['user'])
        self.assertNotIn('token_digest', registration.data)
        self.assertIn('expires_at', registration.data)

        user = User.objects.get(username='alice')
        self.assertTrue(user.check_password('BrightComet!4729'))
        raw_token = registration.data['token']
        session = UserSession.objects.get(user=user)
        self.assertEqual(session.token, hash_token(raw_token))
        self.assertNotEqual(session.token, raw_token)

        login = self.client.post(
            '/auth/login',
            {'username': 'alice', 'password': 'BrightComet!4729'},
            format='json',
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertEqual(login.data['user']['id'], user.id)
        self.assertNotEqual(login.data['token'], raw_token)
        self.assertEqual(UserSession.objects.filter(user=user).count(), 2)

        invalid_login = self.client.post(
            '/auth/login',
            {'username': 'alice', 'password': 'wrong-password'},
            format='json',
        )
        self.assertEqual(invalid_login.status_code, status.HTTP_400_BAD_REQUEST)


class TokenAuthenticationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='token-owner',
            password='FrostedPine!4729',
        )

    def _client_for_token(self, token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
        return client

    def test_missing_valid_expired_and_inactive_tokens(self):
        self.assertEqual(
            self.client.get('/documents').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        raw_token, session = issue_user_session(self.user)
        authenticated_client = self._client_for_token(raw_token)
        self.assertEqual(
            authenticated_client.get('/documents').status_code,
            status.HTTP_200_OK,
        )

        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=['expires_at'])
        self.assertEqual(
            authenticated_client.get('/documents').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        inactive_token, _ = issue_user_session(self.user)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        inactive_client = self._client_for_token(inactive_token)
        self.assertEqual(
            inactive_client.get('/documents').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class DocumentAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='document-owner',
            password='FrostedPine!4729',
        )
        self.other_user = User.objects.create_user(
            username='other-owner',
            password='GraniteCedar!5831',
        )
        token, _ = issue_user_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token}')

    def _other_client(self):
        token, _ = issue_user_session(self.other_user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
        return client

    def test_json_document_crud_and_pagination_shape(self):
        creation = self.client.post(
            '/documents',
            {
                'title': 'Roadmap',
                'content': 'Draft milestones.',
                'owner': self.other_user.id,
            },
            format='json',
        )
        self.assertEqual(creation.status_code, status.HTTP_201_CREATED)
        document_id = creation.data['id']
        self.assertEqual(creation.data['owner'], self.user.id)
        self.assertEqual(Document.objects.get(pk=document_id).owner_id, self.user.id)

        listing = self.client.get('/documents?page=1&page_size=10')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(listing.data.keys()),
            {'count', 'next', 'previous', 'results'},
        )
        self.assertEqual(listing.data['count'], 1)
        self.assertEqual(listing.data['results'][0]['title'], 'Roadmap')

        detail = self.client.get(f'/documents/{document_id}')
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data['content'], 'Draft milestones.')

        replacement = self.client.put(
            f'/documents/{document_id}',
            {'title': 'Updated roadmap', 'content': 'Replacement content.'},
            format='json',
        )
        self.assertEqual(replacement.status_code, status.HTTP_200_OK)
        self.assertEqual(replacement.data['title'], 'Updated roadmap')

        patch = self.client.patch(
            f'/documents/{document_id}',
            {'content': 'Patched content.'},
            format='json',
        )
        self.assertEqual(patch.status_code, status.HTTP_200_OK)
        self.assertEqual(patch.data['content'], 'Patched content.')

        deletion = self.client.delete(f'/documents/{document_id}')
        self.assertEqual(deletion.status_code, status.HTTP_200_OK)
        self.assertEqual(
            deletion.data['message'],
            f'Document with ID {document_id} deleted successfully.',
        )
        self.assertFalse(Document.objects.filter(pk=document_id).exists())
        self.assertEqual(
            self.client.get(f'/documents/{document_id}').status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_file_upload_success_unsupported_type_and_size_limit(self):
        upload = SimpleUploadedFile(
            'meeting_notes.md',
            b'# Meeting\n- discuss launch\n',
            content_type='text/markdown',
        )
        uploaded = self.client.post('/documents', {'file': upload}, format='multipart')
        self.assertEqual(uploaded.status_code, status.HTTP_201_CREATED)
        self.assertEqual(uploaded.data['title'], 'meeting_notes')
        self.assertEqual(uploaded.data['content'], '# Meeting\n- discuss launch\n')

        spoofed_type = SimpleUploadedFile(
            'not_really_text.txt',
            b'%PDF-like payload',
            content_type='application/pdf',
        )
        unsupported = self.client.post(
            '/documents',
            {'file': spoofed_type},
            format='multipart',
        )
        self.assertEqual(unsupported.status_code, status.HTTP_400_BAD_REQUEST)

        too_large = SimpleUploadedFile(
            'large.txt',
            b'x' * (5 * 1024 * 1024 + 1),
            content_type='text/plain',
        )
        oversized = self.client.post('/documents', {'file': too_large}, format='multipart')
        self.assertEqual(
            oversized.status_code,
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    def test_document_ownership_privacy_and_delete_permission(self):
        document = Document.objects.create(
            owner=self.user,
            title='Private document',
            content='Only the owner may read this.',
        )
        other_client = self._other_client()

        listing = other_client.get('/documents')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['count'], 0)
        self.assertEqual(
            other_client.get(f'/documents/{document.id}').status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            other_client.patch(
                f'/documents/{document.id}',
                {'title': 'Attempted edit'},
                format='json',
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            other_client.delete(f'/documents/{document.id}').status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertTrue(Document.objects.filter(pk=document.id).exists())


class QueryAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='query-owner',
            password='FrostedPine!4729',
        )
        self.other_user = User.objects.create_user(
            username='query-other',
            password='GraniteCedar!5831',
        )
        token, _ = issue_user_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token}')

    def _other_client(self):
        token, _ = issue_user_session(self.other_user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
        return client

    def test_query_creation_uses_documented_placeholder(self):
        response = self.client.post(
            '/query',
            {'query_text': 'Summarize the roadmap'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['query_text'], 'Summarize the roadmap')
        self.assertEqual(response.data['result_summary'], QUERY_RESULT_SUMMARY)
        self.assertEqual(response.data['user'], self.user.id)
        self.assertTrue(
            QueryLog.objects.filter(
                user=self.user,
                query_text='Summarize the roadmap',
                result_summary=QUERY_RESULT_SUMMARY,
            ).exists()
        )

    def test_query_pagination_validation_and_owner_isolation(self):
        for index in range(12):
            QueryLog.objects.create(
                user=self.user,
                query_text=f'owner query {index}',
                result_summary=QUERY_RESULT_SUMMARY,
            )
        QueryLog.objects.create(
            user=self.other_user,
            query_text='other private query',
            result_summary=QUERY_RESULT_SUMMARY,
        )

        response = self.client.get('/queries?page=1&page_size=5')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 12)
        self.assertEqual(len(response.data['results']), 5)
        self.assertIsNotNone(response.data['next'])
        self.assertIsNone(response.data['previous'])
        self.assertTrue(
            all(item['user'] == self.user.id for item in response.data['results'])
        )

        invalid_size = self.client.get('/queries?page_size=101')
        self.assertEqual(invalid_size.status_code, status.HTTP_400_BAD_REQUEST)

        other_response = self._other_client().get('/queries')
        self.assertEqual(other_response.status_code, status.HTTP_200_OK)
        self.assertEqual(other_response.data['count'], 1)
        self.assertEqual(
            other_response.data['results'][0]['query_text'],
            'other private query',
        )
