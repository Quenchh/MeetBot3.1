from config import load_settings, _parse_dotenv


def _load(tmp_path, env=None, dotenv=""):
    path = tmp_path / ".env"
    path.write_text(dotenv, encoding="utf-8")
    return load_settings(env=env or {}, dotenv_path=path)


def test_env_overrides_dotenv_and_parses_quotes_and_comments(tmp_path):
    s = _load(
        tmp_path,
        env={"MEETBOT_PORT": "9001"},
        dotenv='MEETBOT_PORT=9000  # yorum\nMEETBOT_ADMIN_PASSWORD="a b#c"\nexport MEETBOT_GUEST_CONTROLS=false\n',
    )
    assert s.port == 9001
    assert s.admin_password == "a b#c"
    assert s.admin_password_generated is False
    assert s.guest_controls is False


def test_missing_password_is_generated(tmp_path):
    s = _load(tmp_path)
    assert s.admin_password_generated is True
    assert len(s.admin_password) >= 12


def test_invalid_and_out_of_range_numbers_fall_back_or_clamp(tmp_path):
    s = _load(tmp_path, env={"MEETBOT_PORT": "abc", "MEETBOT_PLAYLIST_LIMIT": "5000", "MEETBOT_MAX_QUEUE": "-3"})
    assert s.port == 8000
    assert s.playlist_limit == 200
    assert s.max_queue == 1


def test_empty_values_use_defaults(tmp_path):
    s = _load(tmp_path, dotenv="MEETBOT_GUEST_CONTROLS=\nMEETBOT_NORMALIZE=\nMEETBOT_PORT=\nMEETBOT_BOT_NAME=\n")
    assert s.guest_controls is True
    assert s.normalize is True
    assert s.port == 8000
    assert s.bot_name == "MeetBot"


def test_bool_words_and_host_list(tmp_path):
    s = _load(tmp_path, env={"MEETBOT_GUEST_CONTROLS": "Hayır", "MEETBOT_NORMALIZE": "evet",
                             "MEETBOT_ALLOWED_HOSTS": " YouTube.com , youtu.be ,"})
    assert s.guest_controls is False
    assert s.normalize is True
    assert s.allowed_hosts == ("youtube.com", "youtu.be")


def test_relative_dirs_resolve_against_repo(tmp_path):
    s = _load(tmp_path, env={"MEETBOT_DOWNLOADS_DIR": "dl_test"})
    assert s.downloads_dir.is_absolute()
    assert s.downloads_dir.name == "dl_test"


def test_parse_dotenv_ignores_garbage(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# yorum\n\nNOEQUALS\n=novalue\nKEY='x y'\n", encoding="utf-8")
    assert _parse_dotenv(path) == {"": "novalue", "KEY": "x y"}
