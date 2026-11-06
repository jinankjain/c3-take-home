"""Retrieval tests use invented services, independently of practice bug fixes."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.retrieval import Corpus, FileTools
import fixer.retrieval as retrieval


def write(root: Path, path: str, text: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    write(tmp_path, "INDEX.md", """# Platform corpus
| File | Status |
| `platform/accelerator-v1.md` | SUPERSEDED |
| `platform/accelerator-v9.md` | CURRENT |
Services: `service/nebula/`, `service/archive/`
""")
    write(tmp_path, "platform/accelerator-v1.md", "# Accelerator v1\n\nrelease acknowledged frees units.\n" * 15)
    write(tmp_path, "platform/accelerator-v9.md", "# Accelerator v9\n\nrelease acknowledged does not free units until observation.\n")
    write(tmp_path, "service/nebula/architecture.md", "# Quasar design overview\nStatus: CURRENT\n\nQuasar owns leases and schedules accelerator operations.\n")
    write(tmp_path, "service/nebula/deployment.md", "# Quasar deployment constants\nStatus: CURRENT\n\nThe holder drift budget is 0.3 seconds.\n")
    write(tmp_path, "service/nebula/runbooks/overdue-report.md", "# Quasar report retries\nStatus: CURRENT\n\n## Diagnosis\nEvery retry must check the current lease before sending a report.\n")
    write(tmp_path, "service/archive/overview.md", "# Archive design overview\nStatus: CURRENT\n\nArchive stores compressed files for long-term storage.\n")
    write(tmp_path, "background/proceedings/paper.txt", "# Consensus paper\n\nA fencing protocol rejects stale epochs.\n")
    return tmp_path


def test_current_contract_precedes_more_keyword_dense_superseded_contract(corpus_root: Path):
    corpus = Corpus(corpus_root, "nebula")
    results = corpus.search("release acknowledged units", root="platform")
    assert results[0]["path"] == "platform/accelerator-v9.md"
    assert results[0]["status"] == "CURRENT"
    assert any(item["status"] == "SUPERSEDED" for item in results)
    assert corpus.read("platform/accelerator-v1.md")["status"] == "SUPERSEDED"


def test_bootstrap_discovers_unseen_service_from_source_name(corpus_root: Path):
    # No knowledge of A/B or of service-specific runbook filenames is used.
    corpus = Corpus(corpus_root)
    text = corpus.bootstrap("Quasar report retries under expired leases", max_chars=12_000)
    assert "INDEX.md" in text
    assert "service/nebula/architecture.md" in text
    assert "service/nebula/deployment.md" in text
    assert "overdue-report.md" in text
    assert "accelerator-v9.md" in text
    assert "accelerator-v1.md" not in text.replace(corpus.index, "")
    assert "service/archive/overview.md" not in text
    assert "Consensus paper" not in text
    assert len(text) <= 12_000


def test_codebase_selects_its_documents_even_when_other_service_matches(corpus_root: Path):
    corpus = Corpus(corpus_root, "archive")
    results = corpus.search("report lease storage")
    assert results and all(item["path"].startswith("service/archive/") for item in results)


def test_relevant_section_is_ranked_and_keeps_line_numbers(corpus_root: Path):
    path = "service/nebula/protocol.md"
    write(corpus_root, path, "# Protocol\nStatus: CURRENT\n\n## Identity\nKeys are persisted.\n\n## Renewal\nRenew before lease expiry and check every report retry.\n")
    results = Corpus(corpus_root, "nebula").search("renew lease expiry", limit=1)
    assert results[0]["path"] == path
    assert results[0]["start"] == 7
    assert "Renew before lease expiry" in results[0]["text"]
    read = Corpus(corpus_root, "nebula").read(path, start=7, end=8)
    assert read["start"] == 7 and read["end"] == 8
    assert read["text"] == "## Renewal\nRenew before lease expiry and check every report retry.\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../outside.txt"])
def test_file_reads_and_searches_reject_root_escape(tmp_path: Path, path: str):
    files = FileTools(tmp_path)
    with pytest.raises(ValueError):
        files.read(path)
    with pytest.raises(ValueError):
        files.search("token", path=path)


def test_symlink_files_and_directories_are_neither_listed_nor_read(tmp_path: Path):
    write(tmp_path, "src/safe.py", "TOKEN = 1\n")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    write(outside, "secret.txt", "SECRET_TOKEN\n")
    (tmp_path / "src/link.txt").symlink_to(outside / "secret.txt")
    (tmp_path / "src/linked").symlink_to(outside, target_is_directory=True)
    (tmp_path / "src/internal.py").symlink_to(tmp_path / "src/safe.py")
    files = FileTools(tmp_path)
    assert files.list_files() == ["src/safe.py"]
    for path in ("src/link.txt", "src/linked/secret.txt", "src/internal.py"):
        with pytest.raises(ValueError):
            files.read(path)
    assert files.search("SECRET", path="src") == []


def test_navigation_excludes_secrets_generated_files_and_scratch(tmp_path: Path):
    for path in (".env", ".env.production", "secrets/keys.txt", "private.pem", "scratch/leak.py", ".c3-fixer-case/candidate/leak.py", ".git/config", "src/__pycache__/cache.pyc"):
        write(tmp_path, path, "sensitive\n")
    write(tmp_path, "src/z.py", "needle\n")
    write(tmp_path, "src/a.py", "needle\n")
    files = FileTools(tmp_path)
    assert files.list_files() == ["src/a.py", "src/z.py"]
    assert files.list_files("src/*.py", limit=1) == ["src/a.py"]
    assert [item["path"] for item in files.search("needle")] == ["src/a.py", "src/z.py"]
    with pytest.raises(ValueError):
        files.read(".env")


def test_reads_and_searches_have_byte_character_and_match_bounds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_FILE_BYTES", 128)
    monkeypatch.setattr(retrieval, "MAX_SCAN_BYTES", 160)
    write(tmp_path, "src/a.py", "needle\n" * 100 + "NEVER_READ\n")
    write(tmp_path, "src/b.py", "needle\n" * 100)
    files = FileTools(tmp_path)
    # Explicit small output limits remain bounded regardless of file size.
    result = files.read("src/a.py", max_chars=20)
    assert len(result["text"]) == 20 and result["truncated"]
    assert len(files.search("needle", limit=2)) == 2
    assert files.search("NEVER_READ") == []


def test_empty_oversized_and_unsafe_regex_queries_are_rejected(tmp_path: Path):
    write(tmp_path, "src/a.py", "report lease\n")
    files = FileTools(tmp_path)
    for query in ("", " " * 3, "x" * 513):
        with pytest.raises(ValueError):
            files.search(query)
    with pytest.raises(ValueError):
        files.search("(a+)+$", mode="regex")
    with pytest.raises(ValueError):
        files.search("a*a*a*a*a*b", mode="regex")
    with pytest.raises(ValueError):
        files.search("(a|aa)(a|aa)", mode="regex")
    assert files.search("report|renew", mode="regex")[0]["start"] == 1
    assert files.search("^(report|renew)", mode="regex")[0]["start"] == 1
    with pytest.raises(ValueError):
        files.read("src/a.py", start=2, end=1)


def test_background_is_explicit_bounded_and_not_cached(corpus_root: Path):
    corpus = Corpus(corpus_root, "nebula")
    corpus.bootstrap("leases report")
    assert not any(path.startswith("background/") for path in corpus._cache)
    results = corpus.search("fencing stale epochs", root="background/proceedings/paper.txt")
    assert results[0]["path"] == "background/proceedings/paper.txt"
    assert "rejects stale epochs" in results[0]["text"]
    assert not any(path.startswith("background/") for path in corpus._cache)
    with pytest.raises(ValueError):
        corpus.search("token", root="service/../../outside")


def test_explicit_superseded_header_is_preserved_despite_current_index(corpus_root: Path):
    write(corpus_root, "platform/accelerator-v9.md", "# Accelerator v9\nStatus: SUPERSEDED\n\nrelease needs replacement contract.\n")
    assert Corpus(corpus_root).read("platform/accelerator-v9.md")["status"] == "SUPERSEDED"


def test_descriptive_current_word_does_not_invent_document_status(corpus_root: Path):
    write(corpus_root, "service/nebula/unmarked.md", "# Lease view\n\nThe current lease has an epoch.\n")
    assert Corpus(corpus_root).read("service/nebula/unmarked.md")["status"] == "UNKNOWN"


def test_bootstrap_accepts_full_ticket_but_caps_output(corpus_root: Path):
    text = Corpus(corpus_root, "nebula").bootstrap("lease report " * 1000, max_chars=500)
    assert 0 < len(text) <= 500


def test_focused_search_reloads_a_cached_truncated_document(tmp_path: Path, monkeypatch):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/a.md", "# A\nStatus: CURRENT\n" + "x" * 47)
    write(tmp_path, "platform/b.md", "# B\nStatus: CURRENT\n\nunique needle\n")
    monkeypatch.setattr(retrieval, "MAX_SCAN_BYTES", 75)
    corpus = Corpus(tmp_path)
    corpus.search("needle", root="platform")
    assert corpus._cache["platform/b.md"].truncated
    focused = corpus.search("needle", root="platform/b.md")
    assert focused and "unique needle" in focused[0]["text"]
    assert not corpus._cache["platform/b.md"].truncated


def test_explicit_read_reaches_lines_beyond_two_mebibytes(tmp_path: Path):
    write(tmp_path, "src/large.py", "filler\n" * 300_000 + "needle\n")
    result = FileTools(tmp_path).read("src/large.py", start=300_001)
    assert result["text"] == "needle\n"
    assert result["start"] == result["end"] == 300_001
    assert not result["truncated"]


def test_corpus_search_finds_contract_after_eighty_thousand_characters(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/long.md", "# Long contract\nStatus: CURRENT\n\n" + "filler\n" * 12_000 + "uniquecontractneedle is authoritative.\n")
    results = Corpus(tmp_path).search("uniquecontractneedle", root="platform")
    assert results and "uniquecontractneedle is authoritative" in results[0]["text"]
    assert results[0]["start"] > 10_000


def test_search_excerpt_contains_late_match_and_reports_its_actual_lines(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    text = "# Protocol\nStatus: CURRENT\n\n" + ("unrelated " * 10 + "\n") * 80 + "UNIQUE_NEEDLE_TERM identifies the failure.\n"
    write(tmp_path, "platform/protocol.md", text)
    result = Corpus(tmp_path).search("UNIQUE_NEEDLE_TERM", root="platform", limit=1)[0]
    assert "UNIQUE_NEEDLE_TERM" in result["text"]
    assert len(result["text"]) <= 3500
    assert result["start"] > 1 and result["truncated"]
    original = text.splitlines(keepends=True)
    assert result["text"] == "".join(original[result["start"] - 1:result["end"]])


@pytest.mark.parametrize("header", ["**Status:** SUPERSEDED", "> superseded by replacement.md", "Status: superseded"])
def test_formatted_and_lowercase_status_markers_remain_superseded(tmp_path: Path, header: str):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/old.md", "# Old contract\n" + header + "\n\nlease behavior.\n")
    assert Corpus(tmp_path).read("platform/old.md")["status"] == "SUPERSEDED"


def test_service_discovery_uses_shared_metadata_budget_without_poisoning_cache(tmp_path: Path, monkeypatch):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    for index in range(5):
        write(tmp_path, f"service/service{index}/overview.md", f"# Service {index}\nStatus: CURRENT\nlease protocol.\n" + "x" * 20_000)
    monkeypatch.setattr(retrieval, "MAX_METADATA_BYTES", 200)
    monkeypatch.setattr(retrieval, "MAX_SCAN_BYTES", 1024)
    corpus = Corpus(tmp_path)
    read_bytes = []
    original = corpus.files._text
    def tracked(path, max_bytes=retrieval.MAX_FILE_BYTES):
        read_bytes.append(min(path.stat().st_size, max_bytes))
        return original(path, max_bytes)
    monkeypatch.setattr(corpus.files, "_text", tracked)
    roots, consumed = corpus._service_roots("lease", 1024)
    assert roots and consumed == 1000
    assert not corpus._cache
    read_bytes.clear()
    corpus.search("lease")
    assert sum(read_bytes) <= 1024


def test_literal_search_window_contains_match_late_in_long_line(tmp_path: Path):
    write(tmp_path, "src/large.py", "x" * 10_000 + "needle\n")
    result = FileTools(tmp_path).search("needle", path="src")[0]
    assert "needle" in result["text"]
    assert len(result["text"]) <= 1500 and result["truncated"]
