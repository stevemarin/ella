import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from lsprotocol import types
from pygls.lsp.client import LanguageClient

from ella.analysis import Location, Symbol
from ella.lsp import error_range, symbol_at

SERVER = Path(sys.executable).parent / "ella-lsp"
URI = "file:///program.ella"
LOOP_FOREVER = "let x = $\n    while true\n        print(1)\n    quote 1\n"


def show_range(lines: list[str], line: int, column: int) -> tuple[int, int, int, int]:
    where = error_range(lines, line, column)
    return where.start.line, where.start.character, where.end.line, where.end.character


def test_error_range_underlines_a_word_or_one_character() -> None:
    lines = ["let total = count + 1\n", "x = (\n"]
    assert show_range(lines, 1, 13) == (0, 12, 0, 17)  # count
    assert show_range(lines, 1, 19) == (0, 18, 0, 19)  # +
    assert show_range(lines, 2, 5) == (1, 4, 1, 5)  # (


def test_error_range_stays_in_the_source() -> None:
    assert show_range(["x\n"], 1, 2) == (0, 1, 0, 1)  # the end of the line
    assert show_range(["x\n"], 2, 1) == (0, 0, 0, 1)  # past the last line
    assert show_range([], 1, 1) == (0, 0, 0, 0)


def test_symbol_at_includes_the_end_of_a_name() -> None:
    symbols = [Symbol(Location(2, 5, 3), "abc: i64")]
    assert symbol_at(symbols, 1, 4) is symbols[0]
    assert symbol_at(symbols, 1, 7) is symbols[0]  # just after the name, where the cursor often is
    assert symbol_at(symbols, 1, 8) is None
    assert symbol_at(symbols, 0, 4) is None


class Client:
    """An editor talking to `ella-lsp`, keeping the diagnostics it's sent."""

    def __init__(self) -> None:
        self.client = LanguageClient("test", "1")
        self.published: asyncio.Queue[types.PublishDiagnosticsParams] = asyncio.Queue()
        self.version = 0

        @self.client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)  # type: ignore[untyped-decorator]
        def publish(params: types.PublishDiagnosticsParams) -> None:
            self.published.put_nowait(params)

    def open(self, source: str) -> None:
        self.version += 1
        document = types.TextDocumentItem(uri=URI, language_id="ella", version=self.version, text=source)
        self.client.text_document_did_open(types.DidOpenTextDocumentParams(text_document=document))

    def change(self, source: str) -> None:
        self.version += 1
        self.client.text_document_did_change(
            types.DidChangeTextDocumentParams(
                text_document=types.VersionedTextDocumentIdentifier(uri=URI, version=self.version),
                content_changes=[types.TextDocumentContentChangeWholeDocument(text=source)],
            )
        )

    def document(self) -> types.TextDocumentIdentifier:
        return types.TextDocumentIdentifier(uri=URI)

    async def hover(self, line: int, character: int) -> str | None:
        position = types.Position(line=line, character=character)
        result = await self.client.text_document_hover_async(types.HoverParams(self.document(), position))
        if result is None:
            return None
        assert isinstance(result.contents, types.MarkupContent)
        return result.contents.value

    async def definition(self, line: int, character: int) -> tuple[int, int, int, int] | None:
        position = types.Position(line=line, character=character)
        result = await self.client.text_document_definition_async(types.DefinitionParams(self.document(), position))
        if result is None:
            return None
        assert isinstance(result, types.Location)
        where = result.range
        return where.start.line, where.start.character, where.end.line, where.end.character

    async def completions(self, line: int, character: int) -> list[str]:
        position = types.Position(line=line, character=character)
        result = await self.client.text_document_completion_async(types.CompletionParams(self.document(), position))
        assert isinstance(result, types.CompletionList)
        return [item.label for item in result.items if item.kind is not types.CompletionItemKind.Keyword]

    async def semantic_tokens(self) -> list[int]:
        params = types.SemanticTokensParams(self.document())
        result = await self.client.text_document_semantic_tokens_full_async(params)
        assert isinstance(result, types.SemanticTokens)
        return list(result.data)

    async def diagnostics(self) -> list[tuple[int, int, int, int, str]]:
        """The next diagnostics published: each one's range and message."""
        params = await asyncio.wait_for(self.published.get(), 30)
        assert params.uri == URI
        return [
            (d.range.start.line, d.range.start.character, d.range.end.line, d.range.end.character, d.message)
            for d in params.diagnostics
        ]


@asynccontextmanager
async def server(*args: str) -> AsyncIterator[Client]:
    client = Client()
    await client.client.start_io(str(SERVER), *args)
    try:
        await client.client.initialize_async(
            types.InitializeParams(process_id=None, root_uri=None, capabilities=types.ClientCapabilities())
        )
        client.client.initialized(types.InitializedParams())
        yield client
    finally:
        # Even after a failed test, so that `stop`, which waits for the server to exit, returns.
        await client.client.shutdown_async(None)
        client.client.exit(None)
        await client.client.stop()  # type: ignore[no-untyped-call]


def test_server_reports_errors_and_clears_them() -> None:
    async def session() -> None:
        async with server() as client:
            client.open("let x = 1\nprint(x + y)\n")
            assert await client.diagnostics() == [(1, 10, 1, 11, "undefined name 'y'")]
            client.change("let x = 1\nprint(x)\n")
            assert await client.diagnostics() == []

    asyncio.run(session())


def test_server_reports_every_error() -> None:
    async def session() -> None:
        async with server() as client:
            client.open("let x = 1 + true\nprint(nope)\nlet y = (\n")
            assert await client.diagnostics() == [
                (0, 10, 0, 11, "'+' needs two values of the same type, not i64 and bool"),
                (1, 6, 1, 10, "undefined name 'nope'"),
                (2, 8, 2, 9, "'(' is never closed"),
            ]

    asyncio.run(session())


PROGRAM = """struct Point
    x: f64
    y: f64

let scale = (p: Point, k: f64) -> Point => Point(p.x * k, p.y * k)
let twice = scale(Point(1.0, 2.0), 2.0)
"""


def test_server_hover_and_definition() -> None:
    async def session() -> None:
        async with server() as client:
            client.open(PROGRAM)
            assert await client.diagnostics() == []
            assert await client.hover(4, 6) == "```ella\nlet scale: (Point, f64) -> Point\n```"
            assert await client.hover(4, 51) == "```ella\nPoint.x: f64\n```"  # p.x
            assert await client.hover(4, 53) == "```ella\nf64\n```"  # *
            assert await client.hover(3, 0) is None
            assert await client.definition(4, 49) == (4, 13, 4, 14)  # p, its parameter
            assert await client.definition(4, 51) == (1, 4, 1, 5)  # .x, its field
            assert await client.definition(5, 13) == (4, 4, 4, 9)  # scale
            assert await client.definition(4, 16) == (0, 7, 0, 12)  # Point in an annotation

    asyncio.run(session())


def test_server_follows_edits_until_the_next_analysis() -> None:
    async def session() -> None:
        async with server() as client:
            client.open(PROGRAM)
            await client.diagnostics()
            client.change("# a new first line\n" + PROGRAM)
            # Asked before the new analysis: the symbols have moved down a line.
            assert await client.hover(5, 6) == "```ella\nlet scale: (Point, f64) -> Point\n```"

    asyncio.run(session())


def test_server_semantic_tokens() -> None:
    async def session() -> None:
        async with server() as client:
            client.open("let f = (n: i64) -> i64 => n\n")
            await client.diagnostics()
            # f: function declaration; n: parameter declaration; i64 twice: built-in type; n: parameter.
            declaration, readonly, library = 1, 2, 4
            assert await client.semantic_tokens() == [
                *(0, 4, 1, 2, declaration | readonly),
                *(0, 5, 1, 1, declaration | readonly),
                *(0, 3, 3, 9, library),
                *(0, 8, 3, 9, library),
                *(0, 7, 1, 1, readonly),
            ]

    asyncio.run(session())


def test_server_completes_names_and_members() -> None:
    async def session() -> None:
        async with server() as client:
            client.open(PROGRAM + "print(twice.\n")
            await client.diagnostics()
            assert await client.completions(6, 12) == ["x", "y"]
            names = await client.completions(4, 43)  # the start of the body
            assert names[:3] == ["p", "k", "scale"]
            assert "twice" in names

    asyncio.run(session())


def test_server_counts_columns_in_utf16() -> None:
    async def session() -> None:
        async with server() as client:
            client.open('print("😀" + nope)\n')  # the emoji is one character, but two UTF-16 units
            assert await client.diagnostics() == [(0, 13, 0, 17, "undefined name 'nope'")]

    asyncio.run(session())


def test_server_stops_compile_time_code_that_runs_too_long() -> None:
    async def session() -> None:
        async with server("--timeout", "1") as client:
            client.open(LOOP_FOREVER)
            message = "checking took over 1 seconds; does some compile-time code never finish?"
            assert await client.diagnostics() == [(0, 0, 0, 3, message)]
            client.change("let x = $(quote 1)\nprint(x)\n")  # a new worker checks this
            assert await client.diagnostics() == []

    asyncio.run(session())
