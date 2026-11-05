from django.conf import settings
from django.db import models


class Document(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='documents',
    )

    class Meta:
        ordering = ('-uploaded_at', '-id')
        indexes = [
            models.Index(fields=['owner', '-uploaded_at'], name='api_doc_owner_up_idx'),
        ]

    def __str__(self):
        return self.title


class DocumentChunk(models.Model):
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='chunks',
    )
    chunk_index = models.PositiveIntegerField()
    chunk_text = models.TextField()
    embedding = models.JSONField()

    class Meta:
        ordering = ('document_id', 'chunk_index')
        constraints = [
            models.UniqueConstraint(
                fields=('document', 'chunk_index'),
                name='api_doc_chunk_index_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=('document', 'chunk_index'), name='api_chunk_doc_idx'),
        ]


class QueryLog(models.Model):
    query_text = models.TextField()
    result_summary = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='query_logs',
    )

    class Meta:
        ordering = ('-created_at', '-id')
        indexes = [
            models.Index(fields=['user', '-created_at'], name='api_query_user_cr_idx'),
        ]


class UserSession(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_sessions',
    )
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ('-created_at', '-id')
        indexes = [
            models.Index(fields=['user', 'expires_at'], name='api_sess_user_exp_idx'),
        ]
