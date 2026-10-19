import os

HERE = os.path.dirname(__file__)


def page():
    with open(os.path.join(HERE, "..", "app", "webui", "templates", "index.html"), encoding="utf-8") as fh:
        return fh.read()


def test_the_browsers_back_button_closes_the_delete_confirmation_like_cancel():
    p = page()
    assert 'pushLayer("confirm", closeConfirm)' in p
    assert '$("cdNo").onclick = () => backLayer("confirm", closeConfirm);' in p
    assert 'if (e.key === "Escape" && !$("confirm").hidden) { backLayer("confirm", closeConfirm); return; }' in p
    assert 'function closeConfirm() { dropLayer("confirm");' in p       # a close the page makes itself does not touch history


def test_the_browsers_back_button_closes_the_auto_delete_editor_like_cancel():
    p = page()
    assert 'pushLayer("retention", closeRetention)' in p
    assert '$("rNo").onclick = () => backLayer("retention", closeRetention);' in p
    assert 'if (e.key === "Escape" && !$("retPop").hidden) { backLayer("retention", closeRetention); return; }' in p
    assert 'function closeRetention() { dropLayer("retention");' in p     # leaving Trash closes it without a history step
