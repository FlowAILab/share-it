// Pasteboard inspector: prints item count + types + text/file contents per item.
import AppKit
let pb = NSPasteboard.general
print("changeCount:", pb.changeCount)
let items = pb.pasteboardItems ?? []
print("items:", items.count)
for (i, it) in items.enumerated() {
    print("item[\(i)] types:", it.types.map { $0.rawValue })
    if let s = it.string(forType: .string) { print("  string:", s.prefix(80)) }
    if let h = it.string(forType: .html) { print("  html:", h.prefix(80)) }
    if it.data(forType: .rtf) != nil { print("  rtf: <present>") }
    if let f = it.string(forType: .fileURL) { print("  fileURL:", f) }
}
