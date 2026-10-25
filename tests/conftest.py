"""Shared test fixtures.

Run from the repository root, for example in the Home Assistant Docker image:

    docker run --rm --entrypoint sh -v "$PWD":/work ghcr.io/home-assistant/home-assistant:stable \
        -c 'cd /work && pip install -q -r requirements-test.txt && pytest'
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/subzero_libre in every test."""
    return enable_custom_integrations
