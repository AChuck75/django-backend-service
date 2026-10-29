#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            'Could not import Django. Ensure dependencies are installed and the virtual environment is active.'
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
