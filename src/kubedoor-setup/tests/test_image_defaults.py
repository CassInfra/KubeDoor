import pytest

from kubedoor_setup import render


def test_image_block_is_read_as_data_and_explicit_values_win(tmp_path, monkeypatch):
    internal = tmp_path / 'src/kubedoor-setup/resources/kubedoor.defaults.conf'
    internal.parent.mkdir(parents=True)
    internal.write_text('PG_PORT="5432"\n', encoding='utf-8')
    script = tmp_path / 'deploy/install.sh'
    script.parent.mkdir()
    script.write_text(
        '#!/bin/sh\nexit 99\n'
        '# BEGIN KUBEDOOR IMAGE DEFAULTS\n'
        'IMAGE_REPO="registry.example.com/kubedoor"\n'
        'TAG_SETUP="2.3.1"\n'
        '# END KUBEDOOR IMAGE DEFAULTS\n'
        'TAG_SETUP="not-a-default"\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(render, 'ROOT', tmp_path)
    defaults = render.load_defaults({'TAG_SETUP': 'custom'})
    assert defaults == {'PG_PORT': '5432', 'IMAGE_REPO': 'registry.example.com/kubedoor', 'TAG_SETUP': 'custom'}


def test_missing_version_block_fails_instead_of_using_partial_defaults(tmp_path, monkeypatch):
    internal = tmp_path / 'src/kubedoor-setup/resources/kubedoor.defaults.conf'
    internal.parent.mkdir(parents=True)
    internal.write_text('PG_PORT="5432"\n', encoding='utf-8')
    script = tmp_path / 'deploy/install.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\n', encoding='utf-8')
    monkeypatch.setattr(render, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='镜像版本配置区'):
        render.load_defaults()
