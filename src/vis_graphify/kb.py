"""A client for the graphify knowledge base server, `graphify kb serve`.

The server owns the graph, its ontology, the documents and the change history.
This module talks to it over HTTP with urllib and turns the answers into typed
results. It does not import graphify.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_URL = "http://127.0.0.1:7878"
"""The address of `graphify kb serve` with its default port."""

WRITE_HEADER = "X-Graphify-KB"
"""The server accepts a write without a token only when the request has this header."""

ACTOR_HEADER = "X-Graphify-Actor"
"""The server records this value as the author of a change."""


class KBRequestError(RuntimeError):
    """The knowledge base server refused a request or did not answer."""

    def __init__(self, message: str, status: int = 0, location: str = ""):
        super().__init__(message)
        self.status = status
        self.location = location


@dataclass(frozen=True)
class KBServer:
    """A running knowledge base server."""

    url: Annotated[str, "Base URL of the server and its admin UI."]
    graph_path: Annotated[str, "Absolute path of the graph.json that the server edits."]
    nodes: Annotated[int, "Node count, with the knowledge base nodes."]
    edges: Annotated[int, "Edge count, with the knowledge base edges."]
    documents: Annotated[int, "Count of documents."]
    version: Annotated[int, "Change counter of the server."]
    read_only: Annotated[bool, "True when the server refuses writes."]
    started: Annotated[bool, "True when this call started the server, False when it was already running."]
    stopped: Annotated[bool, "True when kb_stop stopped the server. The counts are from just before the stop."] = False
    pid: Annotated[int | None, "Process id of a server that this extension started, else None."] = None
    log_path: Annotated[str, "Log file of a server that this extension started, else empty."] = ""

    def __str__(self) -> str:
        if self.stopped:
            return (
                f"Stopped knowledge base {self.url}. It saved {self.nodes} nodes, {self.edges} edges "
                f"and {self.documents} documents first."
            )
        how = "Started" if self.started else "Using"
        mode = ", read-only" if self.read_only else ""
        plain = (
            " It uses http, so the token and the data travel unencrypted. Use https for a hosted server."
            if self.url.startswith("http://") and not is_loopback_url(self.url)
            else ""
        )
        return (
            f"{how} knowledge base {self.url}{mode}: {self.nodes} nodes, {self.edges} edges, "
            f"{self.documents} documents. Open the URL for the admin UI.{plain}"
        )


@dataclass(frozen=True)
class KBHit:
    """One node or document that matches a search."""

    kind: Annotated[str, "`node` or `document`."]
    id: Annotated[str, "Node id or document id."]
    label: Annotated[str, "Node label or document title."]
    cls: Annotated[str, "Ontology class of the node, or empty."]
    source_file: Annotated[str, "Source file of the node, or empty."]
    score: Annotated[float, "Search score. Higher is better."]

    def __str__(self) -> str:
        where = f" {self.source_file}" if self.source_file else ""
        return f"{self.label} ({self.kind}, {self.cls or '-'}){where} [{self.id}]"


@dataclass(frozen=True)
class KBSearchResult:
    """Nodes and documents that match a query."""

    query: Annotated[str, "The query text."]
    hits: Annotated[tuple[KBHit, ...], "Best matches first."]

    def __str__(self) -> str:
        if not self.hits:
            return f"Nothing in the knowledge base matches {self.query!r}."
        lines = [f"{len(self.hits)} matches for {self.query!r}:"]
        lines += [str(hit) for hit in self.hits]
        return "\n".join(lines)


@dataclass(frozen=True)
class KBEdge:
    """One edge of a knowledge base node."""

    id: Annotated[str, "Edge id. Give it to `update_edge` or `delete_edge`."]
    direction: Annotated[str, "`out` from the node or `in` to the node."]
    relation: Annotated[str, "Relation name, such as `calls` or `mentions`."]
    other: Annotated[str, "Id of the node at the other end."]
    other_label: Annotated[str, "Label of the node at the other end."]
    confidence: Annotated[str, "EXTRACTED, INFERRED or AMBIGUOUS."]
    kb: Annotated[bool, "True when the knowledge base added the edge, False for a built edge."]

    def __str__(self) -> str:
        arrow = "->" if self.direction == "out" else "<-"
        return f"{arrow} {self.relation} {self.other_label} [{self.other}]"


@dataclass(frozen=True)
class KBNode:
    """One node of the knowledge base, with its attributes and edges."""

    id: Annotated[str, "Node id."]
    label: Annotated[str, "Node label."]
    cls: Annotated[str, "Ontology class."]
    kb: Annotated[bool, "True when the knowledge base added the node, False for a built node."]
    attributes: Annotated[dict[str, Any], "All stored attributes of the node."]
    edges: Annotated[tuple[KBEdge, ...], "Outgoing edges first, then incoming edges."]
    out_total: Annotated[int, "Count of all outgoing edges."]
    in_total: Annotated[int, "Count of all incoming edges."]

    def __str__(self) -> str:
        lines = [f"{self.label} ({self.cls}) [{self.id}], {self.out_total} out, {self.in_total} in:"]
        lines += [str(edge) for edge in self.edges[:40]]
        if len(self.edges) > 40:
            lines.append(f"… {len(self.edges) - 40} more in `edges`.")
        return "\n".join(lines)


@dataclass(frozen=True)
class KBChange:
    """The outcome of one undoable change."""

    summary: Annotated[str, "What the change did."]
    version: Annotated[int, "Change counter of the server after the change."]
    results: Annotated[tuple[dict[str, Any], ...], "One stored record for each operation, in order."] = ()
    undone: Annotated[int | None, "History number of the change that an undo reverted, else None."] = None

    def __str__(self) -> str:
        if self.undone is not None:
            return f"Undid change {self.undone}: {self.summary} (version {self.version})."
        return f"{self.summary} (version {self.version}). graphify.kb_undo() reverts it."


@dataclass(frozen=True)
class KBIngestResult:
    """A document that the server made from a file, a URL or text."""

    document_id: Annotated[str, "Id of the new document."]
    title: Annotated[str, "Title of the document."]
    mode: Annotated[str, "`text` (no model) or `llm` (a model extracted nodes and edges)."]
    linked: Annotated[tuple[str, ...], "Labels of the existing nodes that the document mentions."]
    nodes_added: Annotated[tuple[str, ...], "Ids of the nodes that the extraction added."]
    edges_added: Annotated[int, "Count of the edges that the extraction added."]
    notes: Annotated[tuple[str, ...], "Warnings of the ingest, such as a missing model."]

    def __str__(self) -> str:
        text = (
            f"Document {self.title!r} [{self.document_id}] ({self.mode}): {len(self.linked)} linked nodes, "
            f"{len(self.nodes_added)} new nodes, {self.edges_added} new edges."
        )
        return "\n".join([text, *self.notes])


@dataclass(frozen=True)
class KBFinding:
    """One consistency finding against the ontology."""

    severity: Annotated[str, "error, warning or info."]
    code: Annotated[str, "Finding code, such as `missing_property` or `domain_violation`."]
    message: Annotated[str, "Plain description."]
    node: Annotated[str, "Id of the node, or empty."]
    edge: Annotated[str, "Id of the edge, or empty."]

    def __str__(self) -> str:
        return f"{self.severity} {self.code}: {self.message}"


@dataclass(frozen=True)
class KBReport:
    """Consistency of the graph against its ontology."""

    ok: Annotated[bool, "True when there is no error."]
    errors: Annotated[int, "Count of errors."]
    warnings: Annotated[int, "Count of warnings."]
    notes: Annotated[int, "Count of info findings."]
    by_code: Annotated[dict[str, int], "Count of findings for each code."]
    findings: Annotated[tuple[KBFinding, ...], "The first findings, errors first."]
    truncated: Annotated[bool, "True when the server returned only some findings."] = False

    def __str__(self) -> str:
        head = f"{self.errors} errors, {self.warnings} warnings, {self.notes} notes."
        if self.ok and not self.warnings:
            return "Consistent: " + head
        lines = [head] + [str(finding) for finding in self.findings[:40]]
        if self.truncated or len(self.findings) > 40:
            lines.append("… more in `findings` and `by_code`.")
        return "\n".join(lines)


@dataclass(frozen=True)
class KBPublishResult:
    """A static HTML export of the knowledge base."""

    path: Annotated[str, "Absolute path of the HTML file."]
    bytes: Annotated[int, "File size."]

    def __str__(self) -> str:
        return f"Published {self.path} ({self.bytes} bytes). It opens without a server."


def normalize_url(url: str) -> str:
    """The base URL of a server, without a trailing slash, query or fragment.

    A URL without a scheme gets http for this machine, such as `127.0.0.1:7878`, else https.
    Raises ValueError for another scheme, a missing host or a token inside the URL.
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("Give the URL of the knowledge base server, such as https://kb.example.com.")
    if "://" not in text:
        text = ("http://" if is_loopback_url("http://" + text) else "https://") + text
    parts = urlsplit(text)
    if parts.username or parts.password:
        raise ValueError("Do not put the token in the URL. Give it with token=..., or let the form ask for it.")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{url} is not an http or https URL.")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def is_loopback_url(url: str) -> bool:
    """True for a server on this machine: localhost or a loopback address."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib sends the token to the new address and turns a POST into a GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class KBClient:
    """HTTP access to one knowledge base server."""

    url: str = DEFAULT_URL
    token: str | None = field(default=None, repr=False)
    actor: str = "agent"
    timeout_s: float = 60.0
    _opener: Any = field(default=None, repr=False)

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        """Send one request. Returns the JSON answer. Raises KBRequestError with the server message."""
        target = self.url.rstrip("/") + path
        if query:
            target += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json", ACTOR_HEADER: self.actor}
        if method != "GET":
            headers[WRITE_HEADER] = "1"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=data, method=method, headers=headers)
        opener = self._opener or _OPENER.open
        try:
            with opener(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            if 300 <= error.code < 400:
                location = urljoin(target, error.headers.get("Location") or "")
                raise KBRequestError(
                    f"{method} {path}: the server redirects to {location}. Connect with that address.",
                    error.code,
                    location,
                ) from None
            try:
                message = json.loads(raw or b"{}").get("error") or error.reason
            except (ValueError, AttributeError):
                message = (raw or b"").decode(errors="replace")[:500] or error.reason
            if error.code == 401:
                message = f"{message}. Give the token with graphify.kb_connect(url)"
            elif error.code == 403 and "Host header" in str(message):
                message = f"{message}. Start the server with --allow-host {urlsplit(self.url).hostname}"
            elif error.code == 429 and error.headers.get("Retry-After"):
                message = f"{message} (after {error.headers.get('Retry-After')} s)"
            raise KBRequestError(f"{method} {path}: {error.code} {message}", error.code) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            hint = (
                "Start one with graphify.kb_serve()."
                if is_loopback_url(self.url)
                else "Check the URL, and that the server runs and this machine can reach it."
            )
            raise KBRequestError(f"No knowledge base server answers at {self.url} ({reason}). {hint}") from None
        try:
            answer = json.loads(raw or b"{}")
        except ValueError:
            raise KBRequestError(f"{method} {path}: the answer is not JSON.") from None
        return answer if isinstance(answer, dict) else {"value": answer}


def node_path(node_id: str) -> str:
    """The API path of one node."""
    return "/api/nodes/" + quote(node_id, safe="")


def server(answer: dict, stats: dict, url: str, started: bool, pid: int | None = None, log: str = "") -> KBServer:
    """A KBServer from `/api/health` and `/api/stats` answers."""
    return KBServer(
        url=url,
        graph_path=str(stats.get("graph") or ""),
        nodes=int(stats.get("nodes") or 0),
        edges=int(stats.get("edges") or 0),
        documents=int(stats.get("documents") or 0),
        version=int(answer.get("version") or stats.get("version") or 0),
        read_only=bool(answer.get("read_only")),
        started=started,
        pid=pid,
        log_path=log,
    )


def search_result(query: str, answer: dict) -> KBSearchResult:
    """A KBSearchResult from a `/api/search` answer."""
    hits = tuple(
        KBHit(
            kind=str(hit.get("kind") or "node"),
            id=str(hit.get("id") or ""),
            label=str(hit.get("label") or hit.get("title") or hit.get("id") or ""),
            cls=str(hit.get("class") or ""),
            source_file=str(hit.get("source_file") or ""),
            score=float(hit.get("score") or 0),
        )
        for hit in answer.get("results") or ()
    )
    return KBSearchResult(query=query, hits=hits)


def _edges(rows, direction: str) -> tuple[KBEdge, ...]:
    return tuple(
        KBEdge(
            id=str(row.get("id") or ""),
            direction=direction,
            relation=str(row.get("relation") or ""),
            other=str(row.get("other") or ""),
            other_label=str(row.get("other_label") or row.get("other") or ""),
            confidence=str(row.get("confidence") or ""),
            kb=bool(row.get("kb")),
        )
        for row in rows or ()
    )


def node_result(answer: dict) -> KBNode:
    """A KBNode from a `/api/nodes/<id>` answer."""
    data = dict(answer.get("node") or {})
    return KBNode(
        id=str(data.get("id") or ""),
        label=str(data.get("label") or data.get("id") or ""),
        cls=str(answer.get("class") or ""),
        kb=bool(answer.get("kb")),
        attributes=data,
        edges=_edges(answer.get("out"), "out") + _edges(answer.get("in"), "in"),
        out_total=int(answer.get("out_total") or 0),
        in_total=int(answer.get("in_total") or 0),
    )


def change_result(summary: str, answer: dict) -> KBChange:
    """A KBChange from a `/api/batch` or `/api/undo` answer."""
    return KBChange(
        summary=str(answer.get("summary") or summary),
        version=int(answer.get("version") or 0),
        results=tuple(r if isinstance(r, dict) else {"value": r} for r in answer.get("results") or ()),
        undone=answer.get("undone") if isinstance(answer.get("undone"), int) else None,
    )


def ingest_result(answer: dict) -> KBIngestResult:
    """A KBIngestResult from a `/api/ingest` answer."""
    document = answer.get("document") or {}
    return KBIngestResult(
        document_id=str(document.get("id") or ""),
        title=str(document.get("title") or ""),
        mode=str(answer.get("mode") or "text"),
        linked=tuple(str(row.get("label") or row.get("id")) for row in document.get("linked") or ()),
        nodes_added=tuple(str(n.get("id") if isinstance(n, dict) else n) for n in answer.get("nodes_added") or ()),
        edges_added=int(answer.get("edges_added") or 0),
        notes=tuple(str(note) for note in answer.get("notes") or ()),
    )


def report_result(answer: dict) -> KBReport:
    """A KBReport from a `/api/validate` answer."""
    counts = answer.get("counts") or {}
    findings = tuple(
        KBFinding(
            severity=str(f.get("severity") or ""),
            code=str(f.get("code") or ""),
            message=str(f.get("message") or ""),
            node=str(f.get("node") or ""),
            edge=str(f.get("edge") or ""),
        )
        for f in answer.get("findings") or ()
    )
    errors = int(counts.get("error") or 0)
    return KBReport(
        ok=bool(answer.get("ok", errors == 0)),
        errors=errors,
        warnings=int(counts.get("warning") or 0),
        notes=int(counts.get("info") or 0),
        by_code=dict(answer.get("by_code") or {}),
        findings=findings,
        truncated=bool(answer.get("truncated")),
    )
