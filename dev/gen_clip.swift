// Generates a small H.264 MP4 for local UI testing (macOS only, no ffmpeg needed).
// usage: swift dev/gen_clip.swift <out.mp4> <label> <r> <g> <b> [seconds]
import AVFoundation
import CoreGraphics
import CoreText
import Foundation

let args = CommandLine.arguments
guard args.count >= 6 else {
    print("usage: gen_clip.swift out.mp4 label r g b [seconds]")
    exit(1)
}
let out = URL(fileURLWithPath: args[1])
let label = args[2]
let color = (CGFloat(Double(args[3])!), CGFloat(Double(args[4])!), CGFloat(Double(args[5])!))
let seconds = args.count > 6 ? Int(args[6])! : 60
let (w, h, fps) = (480, 360, 10)

try? FileManager.default.removeItem(at: out)
let writer = try! AVAssetWriter(outputURL: out, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: w, AVVideoHeightKey: h,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 250_000],
])
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
    kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
])
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)

func drawText(_ ctx: CGContext, _ text: String, x: CGFloat, y: CGFloat, size: CGFloat) {
    let font = CTFontCreateWithName("Helvetica-Bold" as CFString, size, nil)
    let attrs: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(red: 1, green: 1, blue: 1, alpha: 1),
    ]
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attrs))
    ctx.textPosition = CGPoint(x: x, y: y)
    CTLineDraw(line, ctx)
}

for frame in 0..<(seconds * fps) {
    while !input.isReadyForMoreMediaData { usleep(1000) }
    var pb: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &pb)
    CVPixelBufferLockBaseAddress(pb!, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb!), width: w, height: h, bitsPerComponent: 8,
                        bytesPerRow: CVPixelBufferGetBytesPerRow(pb!), space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue)!
    ctx.setFillColor(CGColor(red: color.0 * 0.35, green: color.1 * 0.35, blue: color.2 * 0.35, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    let t = CGFloat(frame) / CGFloat(seconds * fps)
    ctx.setFillColor(CGColor(red: color.0, green: color.1, blue: color.2, alpha: 1))
    ctx.fill(CGRect(x: t * CGFloat(w - 60), y: 40, width: 60, height: 60))
    drawText(ctx, label, x: 20, y: CGFloat(h) - 50, size: 30)
    let s = frame / fps
    drawText(ctx, String(format: "%02d:%02d", s / 60, s % 60), x: 20, y: CGFloat(h) - 90, size: 24)
    CVPixelBufferUnlockBaseAddress(pb!, [])
    adaptor.append(pb!, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: CMTimeScale(fps)))
}
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
print("wrote \(out.path)")
