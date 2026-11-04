"""A language server for Ella, built on pygls: editors talk to it over stdin and stdout (the Language Server Protocol)."""

import argparse
import asyncio
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.workspace import TextDocument
from pygls.workspace.position_codec import ServerTextPosition, ServerTextRange

from ella.analysis import (
    TOKEN_MODIFIERS,
    TOKEN_TYPES,
    Analysis,
    Completion,
    Location,
    Place,
    Problem,
    Symbol,
    analyze,
    complete,
    follow_edits,
)

DELAY = 0.3  # seconds of no changes before a document is analyzed again
TIMEOUT = 5.0  # seconds an analysis may take before it's stopped

# What an error is underlined from its start: a name or number, else one character.
WORD = re.compile(r"[A-Za-z0-9_]+")

COMPLETION_KINDS = {
    "variable": types.CompletionItemKind.Variable,
    "parameter": types.CompletionItemKind.Variable,
    "function": types.CompletionItemKind.Function,
    "method": types.CompletionItemKind.Method,
    "property": types.CompletionItemKind.Field,
    "struct": types.CompletionItemKind.Struct,
    "enum": types.CompletionItemKind.Enum,
    "enumMember": types.CompletionItemKind.EnumMember,
    "interface": types.CompletionItemKind.Interface,
    "type": types.CompletionItemKind.Class,
    "namespace": types.CompletionItemKind.Module,
    "keyword": types.CompletionItemKind.Keyword,
}

log = logging.getLogger(__name__)


def error_range(lines: Sequence[str], line: int, column: int) -> ServerTextRange:
    """Where to underline an error at `line` and `column` (from 1), in characters from 0."""
    if not lines:
        origin = ServerTextPosition(0, 0)
        return ServerTextRange(origin, origin)
    # An error at the very end of the source may be past the last line.
    row = min(max(line - 1, 0), len(lines) - 1)
    text = lines[row].rstrip("\r\n")
    start = min(max(column - 1, 0), len(text))
    word = WORD.match(text, start)
    end = word.end() if word else min(start + 1, len(text))
    return ServerTextRange(ServerTextPosition(row, start), ServerTextPosition(row, end))


def symbol_range(location: Location) -> ServerTextRange:
    start = ServerTextPosition(location.line - 1, location.column - 1)
    return ServerTextRange(start, ServerTextPosition(start.line, start.character + location.length))


def symbol_at(symbols: list[Symbol], line: int, character: int) -> Symbol | None:
    """The symbol at a position (from 0, in characters), if there is one."""
    for symbol in symbols:
        where = symbol.where
        if where.line == line + 1 and where.column - 1 <= character <= where.column - 1 + where.length:
            return symbol
    return None


def encode_tokens(symbols: list[Symbol], document: TextDocument) -> list[int]:
    """Symbols as LSP semantic tokens: for each, five numbers, relative to the one before."""
    data: list[int] = []
    previous_line, previous_start = 0, 0
    lines = document.lines
    for symbol in symbols:
        if not symbol.kind or symbol.where.line > len(lines):
            continue
        where = document.range_to_client_units(symbol_range(symbol.where))
        line, start = where.start.line, where.start.character
        modifiers = sum(1 << TOKEN_MODIFIERS.index(m) for m in symbol.modifiers)
        delta_start = start - previous_start if line == previous_line else start
        data += [
            line - previous_line,
            delta_start,
            where.end.character - start,
            TOKEN_TYPES.index(symbol.kind),
            modifiers,
        ]
        previous_line, previous_start = line, start
    return data


def start_worker(server: int) -> None:
    """Runs first in the worker process. Anything printed there mustn't reach the editor, and the
    worker must end with the server, even if that is killed."""
    sys.stdout = sys.stderr
    threading.Thread(target=watch_server, args=(server,), daemon=True).start()


def watch_server(server: int) -> None:
    while True:
        time.sleep(1)
        try:
            os.kill(server, 0)  # does the server's process still exist?
        except ProcessLookupError:
            os._exit(0)


def new_executor() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, initializer=start_worker, initargs=(os.getpid(),))


class EllaServer(LanguageServer):
    def __init__(self, timeout: float = TIMEOUT) -> None:
        super().__init__("ella", "0.1.0")
        self.timeout = timeout
        self.executor = new_executor()
        self.working = asyncio.Lock()  # held while the worker runs something
        # Each document's analysis that is waiting for typing to pause. A
        # change restarts the wait; once an analysis has started it is left to finish.
        self.waiting: dict[str, asyncio.Task[None]] = {}
        # Each document's latest symbols, and the source they are for.
        self.symbols: dict[str, tuple[str, list[Symbol]]] = {}

    def schedule(self, uri: str, delay: float) -> None:
        """Analyze a document after `delay` seconds without changes."""
        self.cancel(uri)
        self.waiting[uri] = asyncio.create_task(self.analyze_later(uri, delay))

    def cancel(self, uri: str) -> None:
        if (task := self.waiting.pop(uri, None)) is not None:
            task.cancel()

    async def analyze_later(self, uri: str, delay: float) -> None:
        await asyncio.sleep(delay)
        del self.waiting[uri]
        await self.analyze(uri)

    async def analyze(self, uri: str) -> None:
        async with self.working:
            if uri not in self.workspace.text_documents:
                return  # closed while waiting
            document = self.workspace.get_text_document(uri)
            source, version = document.source, document.version
            try:
                analysis = await self.run(analyze, source, self.place(document))
            except TimeoutError:
                message = f"checking took over {self.timeout:g} seconds; does some compile-time code never finish?"
                analysis = Analysis([Problem(message, 1, 1)], None)
            except Exception as error:  # a bug in the analysis, or the worker died
                log.exception("analysis failed")
                analysis = Analysis([Problem(f"internal error while checking: {error!r}", 1, 1)], None)
        if uri not in self.workspace.text_documents:
            return
        # Publish results for the exact snapshot analyzed, not later edits.
        if analysis.symbols is not None:
            self.symbols[uri] = source, analysis.symbols
        lines = source.splitlines()
        diagnostics = [
            types.Diagnostic(
                range=document.range_to_client_units(error_range(lines, p.line, p.column)),
                message=p.message,
                severity=types.DiagnosticSeverity.Warning if p.warning else types.DiagnosticSeverity.Error,
                source="ella",
            )
            for p in analysis.problems
        ]
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=uri, version=version, diagnostics=diagnostics)
        )
        # Ask the editor to ask for the new highlighting, if it can.
        tokens = self.client_capabilities.workspace and self.client_capabilities.workspace.semantic_tokens
        if analysis.symbols is not None and tokens and tokens.refresh_support:
            self.workspace_semantic_tokens_refresh(None)

    def place(self, document: TextDocument) -> Place:
        """Where a document is, for its imports: its file, the workspace folder, and the open documents' text."""
        open_files = {d.path: d.source for d in self.workspace.text_documents.values() if d.uri.startswith("file:")}
        path = document.path if document.uri.startswith("file:") else None
        return Place(path, self.workspace.root_path, open_files)

    async def run[*Args, R](self, function: Callable[[*Args], R], *args: *Args) -> R:
        """Run a function in the worker, which is replaced if it takes too long or fails."""
        try:
            return await asyncio.wait_for(asyncio.wrap_future(self.executor.submit(function, *args)), self.timeout)
        except Exception:
            self.executor.kill_workers()
            self.executor = new_executor()
            raise

    def current_symbols(self, document: TextDocument) -> list[Symbol]:
        """The latest symbols, moved to follow the edits made since they were found."""
        source, symbols = self.symbols.get(document.uri, ("", []))
        if source != document.source:
            symbols = follow_edits(symbols, source, document.source)
            self.symbols[document.uri] = document.source, symbols
        return symbols

    def symbol_at(self, document: TextDocument, position: types.Position) -> Symbol | None:
        server = document.position_codec.position_from_client_units(document.lines, position)
        return symbol_at(self.current_symbols(document), server.line, server.character)

    async def completions(self, document: TextDocument, position: types.Position) -> list[Completion]:
        server = document.position_codec.position_from_client_units(document.lines, position)
        async with self.working:
            try:
                place = self.place(document)
                return await self.run(complete, document.source, server.line + 1, server.character + 1, place)
            except Exception:
                log.exception("completion failed")
                return []


def make_server(timeout: float = TIMEOUT) -> EllaServer:
    server = EllaServer(timeout)

    @server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    async def did_open(ls: EllaServer, params: types.DidOpenTextDocumentParams) -> None:
        ls.schedule(params.text_document.uri, 0)

    @server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
    async def did_change(ls: EllaServer, params: types.DidChangeTextDocumentParams) -> None:
        # Other open documents may import this one.
        for uri in ls.workspace.text_documents:
            ls.schedule(uri, DELAY)

    @server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    async def did_close(ls: EllaServer, params: types.DidCloseTextDocumentParams) -> None:
        ls.cancel(params.text_document.uri)
        ls.symbols.pop(params.text_document.uri, None)
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
        )

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(ls: EllaServer, params: types.HoverParams) -> types.Hover | None:
        document = ls.workspace.get_text_document(params.text_document.uri)
        symbol = ls.symbol_at(document, params.position)
        if symbol is None:
            return None
        return types.Hover(
            contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=f"```ella\n{symbol.hover}\n```"),
            range=document.range_to_client_units(symbol_range(symbol.where)),
        )

    @server.feature(types.TEXT_DOCUMENT_DEFINITION)
    def definition(ls: EllaServer, params: types.DefinitionParams) -> types.Location | None:
        document = ls.workspace.get_text_document(params.text_document.uri)
        symbol = ls.symbol_at(document, params.position)
        if symbol is None or symbol.definition is None:
            return None
        if symbol.definition.file:  # another file, which may not be open: its range is in characters
            where = symbol_range(symbol.definition)
            start = types.Position(where.start.line, where.start.character)
            end = types.Position(where.end.line, where.end.character)
            return types.Location(Path(symbol.definition.file).as_uri(), types.Range(start, end))
        return types.Location(document.uri, document.range_to_client_units(symbol_range(symbol.definition)))

    @server.feature(
        types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
        types.SemanticTokensLegend(token_types=list(TOKEN_TYPES), token_modifiers=list(TOKEN_MODIFIERS)),
    )
    def semantic_tokens(ls: EllaServer, params: types.SemanticTokensParams) -> types.SemanticTokens:
        document = ls.workspace.get_text_document(params.text_document.uri)
        return types.SemanticTokens(data=encode_tokens(ls.current_symbols(document), document))

    @server.feature(types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(trigger_characters=["."]))
    async def completion(ls: EllaServer, params: types.CompletionParams) -> types.CompletionList:
        document = ls.workspace.get_text_document(params.text_document.uri)
        found = await ls.completions(document, params.position)
        items = [
            types.CompletionItem(label=c.label, kind=COMPLETION_KINDS.get(c.kind), detail=c.detail or None)
            for c in found
        ]
        return types.CompletionList(is_incomplete=False, items=items)

    return server


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: serve over stdin and stdout until the editor says to stop."""
    args = argparse.ArgumentParser(prog="ella-lsp", description="The Ella language server (LSP over stdio).")
    args.add_argument("--timeout", type=float, default=TIMEOUT, help=f"seconds a check may take (default {TIMEOUT:g})")
    options = args.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    make_server(options.timeout).start_io()
    return 0
