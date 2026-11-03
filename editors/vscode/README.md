# Ella for VS Code

* Syntax highlighting for `.ella` files and for `ella` code blocks in
  Markdown (a TextMate grammar in [syntaxes/](syntaxes/)).
* Comments, brackets, auto-closing quotes, indentation and folding
  ([language-configuration.json](language-configuration.json)).
* From the Ella language server ([src/ella/lsp.py](../../src/ella/lsp.py)):
  * Errors as you type: all of them, not just the first, including errors
    in compile-time code.
  * Hover: the type of a name, or of an operator's result.
  * Go to definition (F12) for variables, functions, parameters, fields,
    methods, variants and types.
  * Highlighting by what a name is: parameters, functions, fields, types,
    mutable or not, built in or not.
  * Completion of the names in scope, and of fields and methods after `.`.

## Installing

From the repository's top directory:

```sh
make install-extension   # Python and npm packages, then a link in ~/.vscode-server/extensions (or ~/.vscode/extensions)
```

Then reload VS Code (**Developer: Reload Window**). Or `make package` makes
`dist/ella.vsix`, for `code --install-extension dist/ella.vsix`.

The extension starts the server with `uv run ella-lsp` in the first workspace
folder, so it works when this repository is open. Elsewhere, set
`ella.server.command`, for example to
`["uv", "run", "--project", "/path/to/ella", "ella-lsp"]`. An empty list
turns the server off and keeps the highlighting. **Ella: Restart Language
Server** picks up changes to the server.

## Limits

* The checker stops at the first error in each line of a block, so a line
  with an error tells nothing about what's after the error in it. Other
  errors in a top-level line (such as a function) that has a syntax error
  aren't shown, since they likely follow from it.
* An unclosed bracket is closed at the end of its line, to read on. When a
  later `)` was meant for something else, the errors after it may be odd
  until the bracket is closed.
* Compile-time code (`$` outside quotes) runs as you type. A check that takes
  more than 5 seconds is stopped (`ella-lsp --timeout` changes this).
* The grammar guesses from the text: capitalized names are types, and
  `let f = (...) ->` is a function. The server's highlighting corrects it
  once a file has been checked.
