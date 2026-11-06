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


def test_relevance_only_filters_weak_hits_and_preserves_current_and_direct_evidence(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/current.md", "# Current clock contract\nStatus: CURRENT\nA lease uses the receipt clock.\n")
    write(tmp_path, "platform/direct.md", "# Historical protocol\nA lease revoke operation fences the holder.\n")
    write(tmp_path, "platform/weak.md", "# Historical report\nThe lease appears in an unrelated storage report.\n")
    calls = []
    def reject_weak(query, documents):
        calls.append((query, documents))
        # A classifier cannot suppress a protected path by inventing a decision.
        return {**{item["path"]: False for item in documents}, "platform/current.md": False}
    corpus = Corpus(tmp_path, relevancy=reject_weak)
    results = corpus.search("lease revoke", root="platform")
    assert {item["path"] for item in results} == {"platform/current.md", "platform/direct.md"}
    assert len(calls) == 1 and calls[0][0] == "lease revoke"
    assert [item["path"] for item in calls[0][1]] == ["platform/weak.md"]
    assert set(calls[0][1][0]) == {"path", "title", "status", "text", "content_hash"}
    assert len(calls[0][1][0]["content_hash"]) == 64


def test_current_selected_service_and_literal_single_term_queries_bypass_relevance(corpus_root: Path):
    calls = []
    corpus = Corpus(corpus_root, "nebula", relevancy=lambda query, documents: calls.append(documents))
    assert corpus.search("lease revoke")
    assert calls == []
    # Even an unmarked document is direct evidence for a literal query.
    write(corpus_root, "platform/weak.md", "# Notes\nlease observations from another system.\n")
    assert corpus.search("lease", root="platform")
    assert calls == []


def test_focused_search_and_explicit_read_preserve_the_requested_document(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    path = "platform/weak.md"
    write(tmp_path, path, "# Notes\nlease observations from another mechanism.\n")
    calls = []
    corpus = Corpus(tmp_path, relevancy=lambda query, documents: calls.append(documents))
    assert corpus.search("lease revoke", root=path)
    assert calls == []
    corpus.read(path)
    assert corpus.search("lease revoke", root="platform")
    assert calls == []


@pytest.mark.parametrize("link", ["[Prior detail](weak.md)", "`platform/weak.md`"])
def test_explicit_reference_from_current_contract_cannot_be_filtered(tmp_path: Path, link: str):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/current.md", "# Current contract\nStatus: CURRENT\nLease receipt clock. See " + link + ".\n")
    write(tmp_path, "platform/weak.md", "# Notes\nLease observations from another mechanism.\n")
    calls = []
    corpus = Corpus(tmp_path, relevancy=lambda query, documents: calls.append(documents))
    results = corpus.search("lease revoke", root="platform")
    assert any(item["path"] == "platform/weak.md" for item in results)
    assert calls == []


def test_bootstrap_service_reference_resolves_relative_to_its_document(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "service/nebula/design.md", "# Nebula design overview\nStatus: CURRENT\nLease control. See [history](../../platform/weak.md).\n")
    write(tmp_path, "platform/weak.md", "# Notes\nLease observations from another mechanism.\n")
    corpus = Corpus(tmp_path, "nebula")
    corpus.bootstrap("lease revoke")
    calls = []
    corpus.relevancy = lambda query, documents: calls.append(documents)
    assert corpus.search("lease revoke", root="platform")
    assert calls == []


@pytest.mark.parametrize("verdict", [None, [], True, 0, "unknown"])
def test_unavailable_or_malformed_relevance_falls_back_to_identical_lexical_rows(tmp_path: Path, verdict):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/weak.md", "# Notes\nlease observations from another mechanism.\n")
    lexical = Corpus(tmp_path).search("lease revoke", root="platform")
    corpus = Corpus(tmp_path, relevancy=lambda query, documents: verdict)
    assert corpus.search("lease revoke", root="platform") == lexical


@pytest.mark.parametrize("decision", [None, True, 0, "irrelevant"])
def test_missing_and_uncertain_document_decisions_keep_the_lexical_match(tmp_path: Path, decision):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/weak.md", "# Notes\nlease observations from another mechanism.\n")
    corpus = Corpus(tmp_path, relevancy=lambda query, documents: {documents[0]["path"]: decision})
    assert corpus.search("lease revoke", root="platform")


def test_relevance_exception_and_unknown_path_do_not_suppress_evidence(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/weak.md", "# Notes\nlease observations from another mechanism.\n")
    def unavailable(*_args):
        raise RuntimeError("optional route unavailable")
    corpus = Corpus(tmp_path, relevancy=unavailable)
    assert corpus.search("lease revoke", root="platform")
    corpus.relevancy = lambda query, documents: {"platform/another-module.md": False}
    assert corpus.search("lease revoke", root="platform")


def test_dynamic_callback_can_stay_lexical_at_bootstrap_then_filter_later(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    write(tmp_path, "platform/weak.md", "# Notes\nlease observations from another mechanism.\n")
    enabled = False
    def dynamic(_query, documents):
        return {item["path"]: False for item in documents} if enabled else None
    corpus = Corpus(tmp_path, relevancy=dynamic)
    assert corpus.search("lease revoke", root="platform")
    enabled = True
    assert corpus.search("lease revoke", root="platform") == []


def test_weak_shortlist_is_bounded_and_refills_output_after_rejections(tmp_path: Path):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    for index in range(20):
        write(tmp_path, f"platform/doc-{index:02}.md", "# Notes\nlease observations from another mechanism.\n")
    batches = []
    def reject(query, documents):
        batches.append(documents)
        return {item["path"]: False for item in documents}
    results = Corpus(tmp_path, relevancy=reject).search("lease revoke", root="platform", limit=1)
    assert len(batches) == 1 and len(batches[0]) == retrieval.MAX_RELEVANCE_DOCUMENTS
    assert len(results) == 1 and results[0]["path"] == "platform/doc-08.md"
    assert all(len(item["text"]) <= 3500 for item in batches[0])


def test_incomplete_document_and_catalog_references_are_handled_conservatively(tmp_path: Path, monkeypatch):
    write(tmp_path, "INDEX.md", "# Corpus\nCatalog: `platform/weak.md`\n")
    write(tmp_path, "platform/weak.md", "# Notes\nlease observation.\n" + "filler\n" * 100)
    calls = []
    def reject(query, documents):
        calls.append(documents)
        return {item["path"]: False for item in documents}
    # A catalog mention alone does not make every file a mandatory reference.
    assert Corpus(tmp_path, relevancy=reject).search("lease revoke", root="platform") == []
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(retrieval, "MAX_FILE_BYTES", 64)
    assert Corpus(tmp_path, relevancy=reject).search("lease revoke", root="platform")
    assert calls == []


def test_retained_reference_metadata_is_bounded_and_cannot_escape_the_corpus(tmp_path: Path, monkeypatch):
    write(tmp_path, "INDEX.md", "# Corpus\n")
    monkeypatch.setattr(retrieval, "MAX_RETAINED_PATHS", 4)
    corpus = Corpus(tmp_path)
    corpus._remember_references("service/nebula/design.md", "[bad](../../../../outside.md) [secret](../../platform/.env.md)")
    assert not corpus._retained_paths
    for index in range(10):
        corpus._remember_references("service/nebula/design.md", f"[detail](../../platform/doc-{index}.md)")
    assert len(corpus._retained_paths) == 4
    assert set(corpus._retained_paths) == {f"platform/doc-{index}.md" for index in range(6, 10)}
