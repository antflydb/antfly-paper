// Build as the lib-pdf-bench entry point in a clean Antfly v0.3.2 checkout.
const std = @import("std");
const pdf = @import("antfly_pdf");

pub fn main(init: std.process.Init) !void {
    const a = init.gpa;
    var args = try std.process.Args.Iterator.initAllocator(init.minimal.args, a);
    defer args.deinit();
    _ = args.next();
    const path = args.next() orelse return error.MissingPdf;
    const count = try std.fmt.parseInt(usize, args.next() orelse return error.MissingCount, 10);
    var io = std.Io.Threaded.init(a, .{});
    defer io.deinit();
    const bytes = try std.Io.Dir.cwd().readFileAlloc(io.io(), path, a, .limited(512 * 1024 * 1024));
    defer a.free(bytes);
    var doc = try pdf.reader.Reader.init(a, bytes);
    defer doc.deinit();
    for (1..count + 1) |page| {
        const image = try pdf.renderParsedPagePngAdaptiveWithProfileAlloc(a, &doc, page, 150, 40_000_000, 4096, pdf.ImageSamplingPolicy.auto.profile());
        defer a.free(image.png);
        var digest: [32]u8 = undefined;
        std.crypto.hash.sha2.Sha256.hash(image.png, &digest, .{});
        std.debug.print("page {d} {x}\n", .{ page, &digest });
    }
}
