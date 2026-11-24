import Foundation

@main
enum MessageBodyCheck {
    static func main() throws {
        checkMarkdown()

        let plain = "\nUse `print()` or place ``` inside a sentence.\n"
        let program = "def double(n):\n    return n * 2"
        let cases: [(String, [MessageBlock])] = [
            (plain, [.prose(plain)]),
            ("Passed.\n\n```python\n" + program + "\n```\n\nDone.", [.prose("Passed."), .code(program), .prose("Done.")]),
            ("  ~~~swift\r\n  a\r\n    b\r\n  ~~~~", [.code("a\n  b")]),
            ("````text\n```\n~~~\n````\n```\n```", [.code("```\n~~~"), .code("")]),
            ("```py\n\n  first\n\n  second  \n\n```", [.code("\n  first\n\n  second  \n")]),
            ("Here\n```js\nconst html = '<b>x</b>';\n", [.prose("Here"), .code("const html = '<b>x</b>';\n")]),
            ("    ```example\n    value", [.prose("    ```example\n    value")]),
        ]
        for (text, expected) in cases {
            precondition(MessageBlock.parse(text) == expected)
        }
        if let path = CommandLine.arguments.dropFirst().first {
            let code = try String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(in: .newlines)
            let voiceReply = "2 function tests passed.\n\n```python\n" + code + "\n```"
            precondition(MessageBlock.parse(voiceReply) == [.prose("2 function tests passed."), .code(code)])
        }
        print("Message body checks passed.")
    }

    private static func checkMarkdown() {
        let markdown = MessageMarkdown("""
            ## Résumé

            **Bold**, *italic*, ~~removed~~, and `print()`.
            [Reference][guide] or [local file](file:///tmp/example).

            3. First
               - Nested
            4. Last

            > A quotation.

            ---

            ~~~python
            if True:
                print("**literal**")

            ~~~

            | Item | Count |
            | :--- | ----: |
            | One  |       |
            | Two  | 2     |

            [guide]: https://example.com/guide
            """)
        let kinds: [PresentationIntent.Kind] = [.header(level: 2), .paragraph, .orderedList, .blockQuote,
                                                .thematicBreak, .codeBlock(languageHint: "python")]
        precondition(markdown.blocks.dropLast().map(\.kind) == kinds)
        let prose = markdown.blocks[1].text
        precondition(String(prose.characters) == "Bold, italic, removed, and print().\nReference or local file.")
        let styles: [(String, InlinePresentationIntent)] = [
            ("Bold", .stronglyEmphasized), ("italic", .emphasized), ("removed", .strikethrough), ("print()", .code),
        ]
        for (word, style) in styles {
            precondition(prose[prose.range(of: word)!].inlinePresentationIntent?.contains(style) == true)
        }
        precondition(prose[prose.range(of: "Reference")!].link?.absoluteString == "https://example.com/guide")
        precondition(prose[prose.range(of: "local file")!].link == nil)
        let list = markdown.blocks[2]
        precondition(list.children.map(\.kind) == [.listItem(ordinal: 3), .listItem(ordinal: 4)])
        precondition(list.children[0].children.map(\.kind) == [.paragraph, .unorderedList])
        precondition(markdown.blocks[5].code == "if True:\n    print(\"**literal**\")\n")
        let table = markdown.blocks[6]
        guard case .table(let columns) = table.kind else { preconditionFailure("Expected a table") }
        precondition(columns.map(\.alignment) == [.left, .right])
        precondition(table.children[1].children.map(\.kind) == [.tableCell(columnIndex: 0)])
        precondition(markdown.preview.hasPrefix("Résumé\nBold"))
        precondition(MessageMarkdown("An unfinished **reply").preview == "An unfinished **reply")
    }
}
