// Renders the DMG window background (retina) that tells the user what to do:
// drag the app into Applications, then press ⌥S. Run: swift make-dmg-bg.swift out.png
import Cocoa

let W: CGFloat = 640, H: CGFloat = 400, SCALE: CGFloat = 2
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "dmg-bg.png"

let img = NSImage(size: NSSize(width: W, height: H))
img.lockFocusFlipped(false)
NSGraphicsContext.current?.imageInterpolation = .high

// ground — deep warm charcoal so both light/dark Finder chrome look fine
let bg = NSGradient(colors: [NSColor(calibratedRed: 0.13, green: 0.11, blue: 0.10, alpha: 1),
                             NSColor(calibratedRed: 0.09, green: 0.08, blue: 0.075, alpha: 1)])!
bg.draw(in: NSRect(x: 0, y: 0, width: W, height: H), angle: -90)

let coral = NSColor(calibratedRed: 0.85, green: 0.51, blue: 0.37, alpha: 1)
let ink = NSColor(calibratedWhite: 0.92, alpha: 1)
let faint = NSColor(calibratedWhite: 0.55, alpha: 1)

func draw(_ s: String, _ size: CGFloat, _ color: NSColor, _ weight: NSFont.Weight,
          center x: CGFloat, y: CGFloat, kern: CGFloat = 0) {
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color, .kern: kern]
    let a = NSAttributedString(string: s, attributes: attrs)
    let sz = a.size()
    a.draw(at: NSPoint(x: x - sz.width / 2, y: y))
}

// title
draw("Share-It", 27, ink, .semibold, center: W / 2, y: H - 64)
draw("share any AI-coding session as a link", 13, faint, .regular, center: W / 2, y: H - 88)

// arrow between the two icon slots (icons sit at y≈195 center; 128px icons)
// slots: app at x=180, Applications at x=460 (set by Finder layout in make-release.sh)
let arrowY: CGFloat = 205
let path = NSBezierPath()
path.move(to: NSPoint(x: 268, y: arrowY))
path.line(to: NSPoint(x: 372, y: arrowY))
path.lineWidth = 3
coral.setStroke()
path.stroke()
for dy in [-1, 1] {
    let head = NSBezierPath()
    head.move(to: NSPoint(x: 372, y: arrowY))
    head.line(to: NSPoint(x: 356, y: arrowY + CGFloat(dy) * 11))
    head.lineWidth = 3
    head.lineCapStyle = .round
    head.stroke()
}

// steps
draw("1.  Drag Share-It into Applications", 14, ink, .medium, center: W / 2, y: 96)
draw("2.  Press ⌥S  (Option + S)  —  the palette opens over any app", 14, ink, .medium, center: W / 2, y: 70)
draw("first launch: right-click Share-It.app → Open", 11, faint, .regular, center: W / 2, y: 40)

img.unlockFocus()

// write @2x png
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(W * SCALE), pixelsHigh: Int(H * SCALE),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
rep.size = NSSize(width: W, height: H)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
img.draw(in: NSRect(x: 0, y: 0, width: W, height: H))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
