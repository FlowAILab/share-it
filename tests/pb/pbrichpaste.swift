// Simulate a rich-text app paste (NSTextView = TextEdit/Notes/Mail machinery).
import AppKit
let app = NSApplication.shared
let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 600, height: 400))
tv.isRichText = true
tv.paste(nil)
let s = tv.textStorage!
print("=== NSTextView rich paste result ===")
print("plain text:", s.string.replacingOccurrences(of: "\u{fffc}", with: "[ATTACHMENT]"))
var atts = 0
s.enumerateAttribute(.attachment, in: NSRange(location: 0, length: s.length)) { v, _, _ in
    if v != nil { atts += 1 }
}
print("attachments:", atts)
// And what a PLAIN text view takes:
let tv2 = NSTextView(frame: NSRect(x: 0, y: 0, width: 600, height: 400))
tv2.isRichText = false
tv2.paste(nil)
print("=== plain NSTextView paste ==="); print(tv2.string)
