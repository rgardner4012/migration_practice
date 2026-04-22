import os
import pytest

VCSIM_HOST = os.environ.get("VCSIM_HOST", "127.0.0.1")
VCSIM_PORT = int(os.environ.get("VCSIM_PORT", "8989"))
VCSIM_USER = os.environ.get("VCSIM_USER", "user")


@pytest.fixture(scope="session")
def vcsim_password():
    password = os.environ.get("VSPHERE_PASSWORD")
    if not password:
        pytest.skip(
            "VSPHERE_PASSWORD not set — start vcsim via docker compose and set VSPHERE_PASSWORD=pass"
        )
    return password


@pytest.fixture(scope="session")
def vsphere_session(vcsim_password):
    from discoverVms import VsphereSession

    with VsphereSession(
        host=VCSIM_HOST,
        port=VCSIM_PORT,
        username=VCSIM_USER,
        password=vcsim_password,
        insecure=True,
    ) as session:
        yield session


@pytest.fixture(scope="session")
def vcenter_content(vsphere_session):
    return vsphere_session.content
