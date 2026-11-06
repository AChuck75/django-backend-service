import os

import requests
import streamlit as st


API_BASE_URL = os.environ.get('DJANGO_API_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
REQUEST_TIMEOUT_SECONDS = 60


class APIRequestError(Exception):
    pass


def api_request(method, path, token=None, **kwargs):
    headers = kwargs.pop('headers', {})
    if token:
        headers['Authorization'] = f'Token {token}'
    try:
        response = requests.request(
            method,
            f'{API_BASE_URL}{path}',
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
    except requests.RequestException as error:
        raise APIRequestError(
            f'Could not reach the Django API at {API_BASE_URL}.'
        ) from error

    try:
        body = response.json()
    except ValueError:
        body = response.text
    if not response.ok:
        detail = body.get('detail', body) if isinstance(body, dict) else body
        raise APIRequestError(f'API returned HTTP {response.status_code}: {detail}')
    return body


def _init_session_state():
    defaults = {
        'auth_token': None,
        'username': None,
        'chat_messages': [],
        'uploaded_documents': [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _login_screen():
    st.title('Document GenAI Assistant')
    st.caption('Sign in to upload documents and chat with the assistant.')
    login_tab, register_tab = st.tabs(['Log in', 'Create account'])

    with login_tab:
        with st.form('login_form'):
            username = st.text_input('Username')
            password = st.text_input('Password', type='password')
            submitted = st.form_submit_button('Log in', use_container_width=True)
        if submitted:
            try:
                result = api_request(
                    'POST',
                    '/auth/login',
                    json={'username': username, 'password': password},
                )
                st.session_state.auth_token = result['token']
                st.session_state.username = result['user']['username']
                st.session_state.chat_messages = []
                st.rerun()
            except (APIRequestError, KeyError) as error:
                st.error(str(error) or 'Login response was incomplete.')

    with register_tab:
        with st.form('register_form'):
            new_username = st.text_input('Username', key='register_username')
            email = st.text_input('Email (optional)')
            new_password = st.text_input(
                'Password',
                type='password',
                key='register_password',
            )
            submitted = st.form_submit_button('Create account', use_container_width=True)
        if submitted:
            try:
                result = api_request(
                    'POST',
                    '/auth/register',
                    json={
                        'username': new_username,
                        'email': email,
                        'password': new_password,
                    },
                )
                st.session_state.auth_token = result['token']
                st.session_state.username = result['user']['username']
                st.session_state.chat_messages = []
                st.rerun()
            except (APIRequestError, KeyError) as error:
                st.error(str(error) or 'Registration response was incomplete.')


def _upload_panel():
    st.sidebar.subheader('Add a document')
    upload = st.sidebar.file_uploader(
        'Upload a PDF or TXT file',
        type=['pdf', 'txt'],
        key='document_upload',
    )
    if upload is not None and st.sidebar.button('Upload and index', type='primary'):
        mime_type = upload.type or (
            'application/pdf' if upload.name.lower().endswith('.pdf') else 'text/plain'
        )
        try:
            document = api_request(
                'POST',
                '/documents',
                token=st.session_state.auth_token,
                files={'file': (upload.name, upload.getvalue(), mime_type)},
            )
            ingestion = api_request(
                'POST',
                '/api/ingest-doc',
                token=st.session_state.auth_token,
                json={'document_id': document['id']},
            )
            st.session_state.uploaded_documents.append(
                {
                    'id': document['id'],
                    'title': document['title'],
                    'chunk_count': ingestion['chunk_count'],
                }
            )
            st.sidebar.success(
                f'Ingested "{document["title"]}" into '
                f'{ingestion["chunk_count"]} chunk(s).'
            )
        except (APIRequestError, KeyError) as error:
            st.sidebar.error(str(error) or 'Upload or ingestion response was incomplete.')

    for document in st.session_state.uploaded_documents:
        st.sidebar.caption(
            f'{document["title"]} (ID {document["id"]}, '
            f'{document["chunk_count"]} chunks)'
        )


def _render_chat():
    for message in st.session_state.chat_messages:
        with st.chat_message(message['role']):
            st.markdown(message['content'])
            if message.get('sources'):
                st.markdown('**Sources**')
                for source in message['sources']:
                    st.markdown(
                        f'**{source.get("document_title", "Document")} '
                        f'(ID {source["document_id"]})**'
                    )
                    st.write(source['chunk_text'])

    prompt = st.chat_input('Ask about your documents or the weather')
    if not prompt:
        return

    prior_turns = [
        {'role': message['role'], 'content': message['content']}
        for message in st.session_state.chat_messages
    ]
    st.session_state.chat_messages.append({'role': 'user', 'content': prompt})
    try:
        with st.spinner('Thinking...'):
            result = api_request(
                'POST',
                '/api/assistant-chat',
                token=st.session_state.auth_token,
                json={'prompt': prompt, 'history': prior_turns},
            )
        st.session_state.chat_messages.append(
            {
                'role': 'assistant',
                'content': result['answer'],
                'sources': result.get('sources', []),
            }
        )
    except (APIRequestError, KeyError) as error:
        st.session_state.chat_messages.append(
            {
                'role': 'assistant',
                'content': f'Unable to complete that request: {error}',
                'sources': [],
            }
        )
    st.rerun()


def main():
    st.set_page_config(page_title='Document GenAI Assistant')
    _init_session_state()
    if not st.session_state.auth_token:
        _login_screen()
        return

    st.sidebar.write(f'Signed in as **{st.session_state.username}**')
    if st.sidebar.button('Log out'):
        for key in ('auth_token', 'username', 'chat_messages', 'uploaded_documents'):
            del st.session_state[key]
        st.rerun()

    _upload_panel()
    st.title('Document GenAI Assistant')
    st.caption(
        'Ask questions about your indexed documents, continue a conversation, '
        'or ask for current weather.'
    )
    _render_chat()


if __name__ == '__main__':
    main()
