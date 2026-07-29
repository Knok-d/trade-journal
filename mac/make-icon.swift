// Рисует иконку приложения и складывает набор PNG для iconutil.
//
// Иконка рисуется кодом, а не лежит картинкой: бинарных файлов в репозитории
// нет ни одного, и заводить первый ради иконки, которую всё равно надо
// пересобирать под каждый размер, незачем. Заодно её видно в диффе.
//
// Мотив — две свечи: зелёная и красная. Читается на 16 пикселях, чего не даёт
// ни график линией, ни монограмма.

import AppKit

let outputDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset"

let background = NSColor(red: 0.055, green: 0.071, blue: 0.137, alpha: 1)   // #0e1223
let deep = NSColor(red: 0.008, green: 0.023, blue: 0.090, alpha: 1)         // #020617
let green = NSColor(red: 0.133, green: 0.773, blue: 0.369, alpha: 1)        // #22c55e
let red = NSColor(red: 0.937, green: 0.267, blue: 0.267, alpha: 1)          // #ef4444

/// Координаты задаются долями холста, но все размеры переводятся в пиксели
/// до любых сравнений: минимальная толщина фитиля — это «не тоньше пикселя»,
/// и в долях такое сравнение молча раздувает фитиль во весь холст.
func candle(x: CGFloat, width: CGFloat, bodyTop: CGFloat, bodyBottom: CGFloat,
            wickTop: CGFloat, wickBottom: CGFloat, color: NSColor, size: CGFloat) {
    color.setFill()

    let bodyWidth = width * size
    let bodyX = x * size
    let wickWidth = max(1, bodyWidth * 0.22)
    NSBezierPath(rect: NSRect(x: bodyX + bodyWidth / 2 - wickWidth / 2,
                              y: wickBottom * size,
                              width: wickWidth,
                              height: (wickTop - wickBottom) * size)).fill()

    let body = NSRect(x: bodyX, y: bodyBottom * size,
                      width: bodyWidth, height: (bodyTop - bodyBottom) * size)
    let corner = min(size * 0.02, bodyWidth / 4)
    NSBezierPath(roundedRect: body, xRadius: corner, yRadius: corner).fill()
}

func draw(size: CGFloat) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    // Подложка-«скруглённый квадрат» рисуется сама: iconutil маску не применяет.
    // Поля по краям — так же, как у системных иконок, иначе значок выглядит
    // крупнее соседей в Dock.
    let inset = size * 0.06
    let plate = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
    let radius = plate.width * 0.225
    let shape = NSBezierPath(roundedRect: plate, xRadius: radius, yRadius: radius)
    shape.addClip()
    NSGradient(starting: background, ending: deep)!.draw(in: plate, angle: -90)

    candle(x: 0.30, width: 0.13, bodyTop: 0.70, bodyBottom: 0.42,
           wickTop: 0.80, wickBottom: 0.32, color: green, size: size)
    candle(x: 0.57, width: 0.13, bodyTop: 0.58, bodyBottom: 0.30,
           wickTop: 0.68, wickBottom: 0.22, color: red, size: size)

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

try? FileManager.default.createDirectory(atPath: outputDir,
                                         withIntermediateDirectories: true)

// Набор размеров задан форматом iconset: без любого из них iconutil ругается.
for (base, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
                      (256, 1), (256, 2), (512, 1), (512, 2)] {
    let pixels = CGFloat(base * scale)
    let suffix = scale == 2 ? "@2x" : ""
    let name = "\(outputDir)/icon_\(base)x\(base)\(suffix).png"
    try! draw(size: pixels).write(to: URL(fileURLWithPath: name))
}

print("иконки: \(outputDir)")
