from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DocumentChunk',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('chunk_index', models.PositiveIntegerField()),
                ('chunk_text', models.TextField()),
                ('embedding', models.JSONField()),
                (
                    'document',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='chunks',
                        to='api.document',
                    ),
                ),
            ],
            options={
                'ordering': ('document_id', 'chunk_index'),
            },
        ),
        migrations.AddConstraint(
            model_name='documentchunk',
            constraint=models.UniqueConstraint(
                fields=('document', 'chunk_index'),
                name='api_doc_chunk_index_uniq',
            ),
        ),
        migrations.AddIndex(
            model_name='documentchunk',
            index=models.Index(
                fields=['document', 'chunk_index'],
                name='api_chunk_doc_idx',
            ),
        ),
    ]
