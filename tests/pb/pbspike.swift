// Spike: ONE write = rich-text item + N file items (Send result candidate shape)
import AppKit
let pb = NSPasteboard.general
pb.prepareForNewContents(with: .currentHostOnly)
var items: [NSPasteboardWriting] = []
let t = NSPasteboardItem()
let html = "<p style=\"font:14px -apple-system\"><b>Research done.</b> Market map with <code>3 segments</code> is attached — summary in REPORT.md.</p>"
t.setString(html, forType: .html)
if let attr = try? NSAttributedString(data: Data(html.utf8),
        options: [.documentType: NSAttributedString.DocumentType.html,
                  .characterEncoding: String.Encoding.utf8.rawValue],
        documentAttributes: nil),
   let rtf = attr.rtf(from: NSRange(location: 0, length: attr.length), documentAttributes: [:]) {
    t.setData(rtf, forType: .rtf)
}
t.setString("Research done. Market map with 3 segments is attached — summary in REPORT.md.", forType: .string)
items.append(t)
for p in CommandLine.arguments.dropFirst() { items.append(URL(fileURLWithPath: p) as NSURL) }
let ok = pb.writeObjects(items)
print("writeObjects:", ok)
