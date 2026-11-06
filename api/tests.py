import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .ai import cosine_similarity, create_embeddings, generate_answer
from .models import Document, DocumentChunk, QueryLog, UserSession
from .views import QUERY_RESULT_SUMMARY, hash_token, issue_user_session
from .weather import fetch_current_weather

User = get_user_model()


def _minimal_text_pdf(text):
    escaped_text = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = f'BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET'.encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>'
        ),
        (
            b'<< /Length ' + str(len(stream)).encode('ascii') + b' >>\n'
            b'stream\n' + stream + b'\nendstream'
        ),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    pdf = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f'{index} 0 obj\n'.encode('ascii'))
        pdf.extend(body)
        pdf.extend(b'\nendobj\n')
    xref_offset = len(pdf)
    pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode('ascii'))
    pdf.extend(
        (
            f'trailer\n<< /Size 6 /Root 1 0 R >>\n'
            f'startxref\n{xref_offset}\n%%EOF\n'
        ).encode('ascii')
    )
    return bytes(pdf)


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

    def test_pdf_upload_extracts_text_for_document_ingestion(self):
        upload = SimpleUploadedFile(
            'project_plan.pdf',
            _minimal_text_pdf('The launch milestone is scheduled for June.'),
            content_type='application/pdf',
        )
        response = self.client.post('/documents', {'file': upload}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['title'], 'project_plan')
        self.assertIn('launch milestone', response.data['content'])
        ingestion = self.client.post(
            '/api/ingest-doc',
            {'document_id': response.data['id']},
            format='json',
        )
        self.assertEqual(ingestion.status_code, status.HTTP_200_OK)
        self.assertEqual(ingestion.data['chunk_count'], 1)

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


class AIAssignmentEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='ai-owner',
            password='FrostedPine!4729',
        )
        self.other_user = User.objects.create_user(
            username='ai-other',
            password='GraniteCedar!5831',
        )
        token, _ = issue_user_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token}')

    def _other_client(self):
        token, _ = issue_user_session(self.other_user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
        return client

    @patch('api.views.generate_answer', return_value='A generated response.')
    def test_ask_ai_uses_template_and_saves_owner_history(self, generate_answer):
        response = self.client.post(
            '/api/ask-ai',
            {'prompt': 'Summarize this plan.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'answer': 'A generated response.'})
        system_prompt, user_prompt = generate_answer.call_args.args
        self.assertIn('helpful assistant', system_prompt)
        self.assertIn('Please respond to this user request:', user_prompt)
        self.assertIn('Summarize this plan.', user_prompt)
        history = self.client.get('/api/query-history?page=1&page_size=5')
        self.assertEqual(history.status_code, status.HTTP_200_OK)
        self.assertEqual(history.data['count'], 1)
        self.assertEqual(history.data['results'][0]['query_text'], 'Summarize this plan.')

    @patch('api.views.create_embeddings')
    def test_ingestion_chunks_and_replaces_document_vectors(self, create_embeddings):
        document = Document.objects.create(
            owner=self.user,
            title='Long document',
            content=' '.join(f'word{index}' for index in range(501)),
        )
        create_embeddings.side_effect = lambda texts: [[1.0, 0.0] for _ in texts]

        response = self.client.post(
            '/api/ingest-doc',
            {'document_id': document.id},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'status': 'success', 'chunk_count': 2})
        chunks = list(DocumentChunk.objects.filter(document=document))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0].chunk_text.split()), 500)
        self.assertEqual(len(chunks[1].chunk_text.split()), 51)
        self.assertEqual(chunks[0].chunk_text.split()[-50:], chunks[1].chunk_text.split()[:50])

        self.client.post(
            '/api/ingest-doc',
            {'document_id': document.id},
            format='json',
        )
        self.assertEqual(DocumentChunk.objects.filter(document=document).count(), 2)
        self.assertEqual(
            self._other_client().post(
                '/api/ingest-doc',
                {'document_id': document.id},
                format='json',
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    @patch('api.views.generate_answer', return_value='The document says launch in June.')
    @patch('api.views.create_embeddings', return_value=[[1.0, 0.0]])
    def test_rag_returns_relevant_owner_scoped_source_chunks(
        self,
        create_embeddings,
        generate_answer,
    ):
        owned_document = Document.objects.create(
            owner=self.user,
            title='Owned',
            content='Owned content.',
        )
        other_document = Document.objects.create(
            owner=self.other_user,
            title='Other',
            content='Private other-user content.',
        )
        DocumentChunk.objects.create(
            document=owned_document,
            chunk_index=0,
            chunk_text='The launch is planned for June.',
            embedding=[0.9, 0.1],
        )
        DocumentChunk.objects.create(
            document=owned_document,
            chunk_index=1,
            chunk_text='The budget is confidential.',
            embedding=[0.0, 1.0],
        )
        DocumentChunk.objects.create(
            document=other_document,
            chunk_index=0,
            chunk_text='Other user private information.',
            embedding=[1.0, 0.0],
        )

        response = self.client.post(
            '/api/rag-query',
            {'query': 'When is launch?'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['answer'], 'The document says launch in June.')
        self.assertEqual(
            response.data['sources'],
            [
                {
                    'document_id': owned_document.id,
                    'chunk_text': 'The launch is planned for June.',
                },
                {
                    'document_id': owned_document.id,
                    'chunk_text': 'The budget is confidential.',
                },
            ],
        )
        system_prompt, user_prompt = generate_answer.call_args.args
        self.assertIn('only the supplied document context', system_prompt)
        self.assertIn('The launch is planned for June.', user_prompt)
        self.assertNotIn('Other user private information.', user_prompt)
        create_embeddings.assert_called_once_with(['When is launch?'])
        self.assertEqual(
            self._other_client().get('/api/query-history').data['count'],
            0,
        )

    @patch('api.agent.generate_answer', return_value='Both documents describe launch timing.')
    def test_assistant_retrieves_chunks_from_multiple_ingested_documents(
        self,
        generate_answer,
    ):
        documents = [
            Document.objects.create(
                owner=self.user,
                title='Mission Alpha',
                content='The spacecraft launch schedule is planned for June.',
            ),
            Document.objects.create(
                owner=self.user,
                title='Mission Beta',
                content='The second mission launch schedule is planned for July.',
            ),
        ]
        for document in documents:
            response = self.client.post(
                '/api/ingest-doc',
                {'document_id': document.id},
                format='json',
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            '/api/assistant-chat',
            {'prompt': 'What is the launch schedule?'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['answer'], 'Both documents describe launch timing.')
        self.assertEqual(
            {source['document_id'] for source in response.data['sources']},
            {document.id for document in documents},
        )
        generate_answer.assert_called_once()


class AssistantAgentEndToEndTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='agent-user',
            password='FrostedPine!4729',
        )
        token, _ = issue_user_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token}')

    @patch('api.agent.generate_answer', return_value='Boston is sunny and 18 C.')
    @patch(
        'api.agent.fetch_current_weather',
        return_value='Current weather for Boston: clear sky; 18 C.',
    )
    def test_weather_agent_routes_invokes_tool_and_uses_session_context(
        self,
        fetch_weather,
        generate_answer,
    ):
        response = self.client.post(
            '/api/assistant-chat',
            {
                'history': [
                    {'role': 'user', 'content': 'What is the weather in Boston?'},
                    {'role': 'assistant', 'content': 'I can check the weather.'},
                ],
                'prompt': 'What about tomorrow?',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['route'], 'weather')
        self.assertEqual(response.data['answer'], 'Boston is sunny and 18 C.')
        self.assertEqual(response.data['sources'], [])
        fetch_weather.assert_called_once()
        self.assertIn('Boston', fetch_weather.call_args.args[0])
        self.assertIn(
            'What is the weather in Boston?',
            generate_answer.call_args.args[1],
        )
        self.assertTrue(
            QueryLog.objects.filter(
                user=self.user,
                query_text='What about tomorrow?',
                result_summary='Boston is sunny and 18 C.',
            ).exists()
        )


class WeatherToolTests(APITestCase):
    @patch('api.weather._get_json')
    def test_weather_tool_fetches_tomorrow_forecast_from_open_meteo(self, get_json):
        get_json.side_effect = [
            {
                'results': [
                    {
                        'name': 'Boston',
                        'admin1': 'Massachusetts',
                        'country': 'United States',
                        'latitude': 42.36,
                        'longitude': -71.06,
                    }
                ]
            },
            {
                'current': {},
                'current_units': {},
                'daily': {
                    'time': ['2026-11-05', '2026-11-06'],
                    'weather_code': [0, 63],
                    'temperature_2m_max': [12, 10],
                    'temperature_2m_min': [5, 3],
                    'precipitation_probability_max': [0, 80],
                },
                'daily_units': {
                    'temperature_2m_max': '°C',
                    'temperature_2m_min': '°C',
                },
            },
        ]

        result = fetch_current_weather('What is the weather in Boston tomorrow?')

        self.assertIn('Tomorrow forecast for Boston, Massachusetts, United States', result)
        self.assertIn('moderate rain', result)
        self.assertIn('high 10 °C', result)
        self.assertIn('80%', result)
        self.assertEqual(get_json.call_count, 2)


class ClaudeIntegrationTests(APITestCase):
    @override_settings(ANTHROPIC_API_KEY='test-claude-key')
    @patch('api.ai.urlopen')
    def test_answer_uses_claude_messages_api(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {'content': [{'type': 'text', 'text': 'Claude response.'}]}
        ).encode('utf-8')
        urlopen.return_value = response

        answer = generate_answer('System instructions.', 'User question.')

        self.assertEqual(answer, 'Claude response.')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(request.get_header('X-api-key'), 'test-claude-key')
        self.assertEqual(request.get_header('Anthropic-version'), '2023-06-01')
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(payload['system'], 'System instructions.')
        self.assertEqual(
            payload['messages'],
            [{'role': 'user', 'content': 'User question.'}],
        )

    def test_local_embeddings_are_deterministic_and_rank_shared_terms(self):
        first, similar, unrelated = create_embeddings(
            ['launch date is in June', 'what is the launch date', 'recipe for apple pie']
        )

        self.assertEqual(first, create_embeddings(['launch date is in June'])[0])
        self.assertGreater(
            cosine_similarity(first, similar),
            cosine_similarity(first, unrelated),
        )
