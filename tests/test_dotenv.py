from hunter.dotenv import load_dotenv


def test_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEP", "shell")
    monkeypatch.delenv("NEWKEY", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    f = tmp_path / ".env"
    f.write_text("# c\nNEWKEY=abc\nexport QUOTED=\"x y\"\nKEEP=file\nEMPTY=\n")
    assert sorted(load_dotenv(f)) == ["NEWKEY", "QUOTED"]
    import os
    assert os.environ["NEWKEY"] == "abc" and os.environ["QUOTED"] == "x y" and os.environ["KEEP"] == "shell"
    monkeypatch.delenv("NEWKEY")
    monkeypatch.delenv("QUOTED")
