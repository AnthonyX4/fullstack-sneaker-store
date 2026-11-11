from io import BytesIO

from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image

import image_utils
import models
from config import settings
from conftest import place_order

# s3_bucket (conftest.py, autouse) is a dict of {key: bytes} standing in for the real bucket


def key(filename: str) -> str:
    return f"profile_pics/{filename}"


def make_image(mode="RGB", size=(640, 480), fmt="PNG") -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, "red").save(buffer, fmt)
    return buffer.getvalue()


def me(client, headers) -> dict:
    return client.get("/api/users/me", headers=headers).json()


def upload(client, headers, user_id, content, filename="pic.png"):
    return client.patch(
        f"/api/users/{user_id}/picture",
        files={"file": (filename, content, "image/png")},
        headers=headers,
    )


def s3_error(*_args):
    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject")


def test_new_user_has_default_picture(client, user_headers):
    user = me(client, user_headers)
    assert user["image_file"] is None
    assert user["image_path"] == "/static/profile_pics/default.jpg"


def test_upload_saves_300px_jpeg_to_s3(client, user_headers, s3_bucket):
    user_id = me(client, user_headers)["user_id"]

    response = upload(client, user_headers, user_id, make_image(mode="RGBA")) # RGBA has to be converted for jpeg
    assert response.status_code == 200
    body = response.json()
    assert body["image_file"].endswith(".jpg")
    assert body["image_path"] == (
        f"https://{settings.s3_bucket_name}.s3.{settings.s3_region}.amazonaws.com/profile_pics/{body['image_file']}"
    )

    with Image.open(BytesIO(s3_bucket[key(body["image_file"])])) as saved:
        assert saved.format == "JPEG"
        assert saved.size == (300, 300)


def test_replacing_picture_deletes_old_object(client, user_headers, s3_bucket):
    user_id = me(client, user_headers)["user_id"]
    first = upload(client, user_headers, user_id, make_image()).json()["image_file"]
    second = upload(client, user_headers, user_id, make_image()).json()["image_file"]

    assert first != second
    assert list(s3_bucket) == [key(second)]


def test_upload_rejects_non_image(client, user_headers, s3_bucket):
    user_id = me(client, user_headers)["user_id"]
    response = upload(client, user_headers, user_id, b"not an image", filename="notes.txt")
    assert response.status_code == 400
    assert s3_bucket == {}


def test_upload_rejects_too_large(client, user_headers, monkeypatch, s3_bucket):
    monkeypatch.setattr(settings, "max_upload_size_bytes", 10)
    user_id = me(client, user_headers)["user_id"]
    response = upload(client, user_headers, user_id, make_image())
    assert response.status_code == 400
    assert "too large" in response.json()["detail"]
    assert s3_bucket == {}


def test_s3_upload_failure_keeps_current_picture(client, user_headers, s3_bucket, monkeypatch):
    user_id = me(client, user_headers)["user_id"]
    current = upload(client, user_headers, user_id, make_image()).json()["image_file"]

    monkeypatch.setattr(image_utils, "_upload_to_s3", s3_error)
    response = upload(client, user_headers, user_id, make_image())
    assert response.status_code == 500
    assert me(client, user_headers)["image_file"] == current
    assert list(s3_bucket) == [key(current)]


def test_s3_unreachable_returns_500(client, user_headers, monkeypatch):
    # BotoCoreError (no answer from AWS at all) is handled the same as a ClientError
    def unreachable(*_args):
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    monkeypatch.setattr(image_utils, "_upload_to_s3", unreachable)
    user_id = me(client, user_headers)["user_id"]
    assert upload(client, user_headers, user_id, make_image()).status_code == 500
    assert me(client, user_headers)["image_file"] is None


def test_s3_delete_failure_doesnt_fail_the_request(client, user_headers, monkeypatch):
    # the old object is deleted after the commit, so the change already happened, an orphan object is the only cost
    user_id = me(client, user_headers)["user_id"]
    upload(client, user_headers, user_id, make_image())

    monkeypatch.setattr(image_utils, "_delete_from_s3", s3_error)
    response = client.delete(f"/api/users/{user_id}/picture", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["image_file"] is None


def test_cannot_upload_for_another_user(client, user_headers):
    user_id = me(client, user_headers)["user_id"]
    response = upload(client, user_headers, user_id + 1, make_image())
    assert response.status_code == 403


def test_upload_requires_auth(client):
    assert upload(client, {}, 1, make_image()).status_code == 401


def test_patch_user_ignores_image_file(client, user_headers):
    # image_file is deliberately not in UserUpdate, otherwise a user could point at
    # someone else's file and get it deleted on their next upload
    user_id = me(client, user_headers)["user_id"]
    response = client.patch(f"/api/users/{user_id}", json={"image_file": "someone-else.jpg"}, headers=user_headers)
    assert response.status_code == 200
    assert response.json()["image_file"] is None


def test_delete_picture(client, user_headers, s3_bucket):
    user_id = me(client, user_headers)["user_id"]
    upload(client, user_headers, user_id, make_image())

    response = client.delete(f"/api/users/{user_id}/picture", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["image_path"] == "/static/profile_pics/default.jpg"
    assert s3_bucket == {}


def test_delete_picture_when_none(client, user_headers):
    user_id = me(client, user_headers)["user_id"]
    assert client.delete(f"/api/users/{user_id}/picture", headers=user_headers).status_code == 400


def test_hard_delete_user_removes_picture(client, user_headers, s3_bucket):
    user_id = me(client, user_headers)["user_id"]
    upload(client, user_headers, user_id, make_image())

    assert client.delete(f"/api/users/{user_id}", headers=user_headers).status_code == 204
    assert s3_bucket == {}


def test_soft_delete_user_removes_picture(client, user_headers, product, s3_bucket, db):
    user_id = me(client, user_headers)["user_id"]
    place_order(client, user_headers, product.product_id) # having an order makes the delete a soft delete
    upload(client, user_headers, user_id, make_image())

    assert client.delete(f"/api/users/{user_id}", headers=user_headers).status_code == 204
    assert s3_bucket == {}
    assert db.get(models.User, user_id).image_file is None
