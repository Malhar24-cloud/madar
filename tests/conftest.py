import pytest
from flask import g
from flask.testing import FlaskClient

from madar import create_app
from madar.cli import DEMO_PASSWORD, seed_data
from madar.config import TestConfig
from madar.models import db


class IsolatedClient(FlaskClient):
    """الاختبارات تبقي سياق التطبيق مفتوحًا للاستعلام من القاعدة، فنمسح المستخدم المخزن في g قبل كل طلب
    حتى لا يتسرب مستخدم عميل اختبار إلى عميل آخر."""
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    app.test_client_class = IsolatedClient
    with app.app_context():
        seed_data()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email, password=DEMO_PASSWORD):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def outbox(app):
    return app.extensions.get("outbox", [])
