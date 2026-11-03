/* Brother DCP-T236 Mac bridge, 2026-11-03.
 * PDF -> PJL/PWG Raster -> Apple's USB backend, without Linux at runtime.
 */
#include <CoreGraphics/CoreGraphics.h>
#include <CoreFoundation/CoreFoundation.h>
#include <cups/raster.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;
static volatile sig_atomic_t cancelled = 0;
static volatile sig_atomic_t child = 0;
static char temporary[PATH_MAX];
static const unsigned W = 2480, H = 3507, STRIDE = 7440;

static void cleanup(void) { if (temporary[0]) unlink(temporary); }
static void cancel_print(int sig) {
  (void)sig;
  cancelled = 1;
  if (child > 0) kill((pid_t)child, SIGTERM);
}
static void fail(const char *message) {
  fprintf(stderr, "ERROR: %s\n", message);
  exit(1);
}
static void write_all(int fd, const void *data, size_t length) {
  const unsigned char *p = data;
  while (length) {
    if (cancelled) fail("Print job cancelled.");
    ssize_t n = write(fd, p, length);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) fail("Could not write printer data.");
    p += n;
    length -= (size_t)n;
  }
}
static int copies_requested(void) {
  const char *text = getenv("IPP_COPIES");
  if (!text) return 1;
  char *end;
  long n = strtol(text, &end, 10);
  if (*end || n < 1 || n > 99) fail("Copies must be between 1 and 99.");
  return (int)n;
}
static bool selected_page(size_t page) {
  const char *ranges = getenv("IPP_PAGE_RANGES");
  if (!ranges || !*ranges) return true;
  while (*ranges) {
    char *end;
    long first = strtol(ranges, &end, 10), last = first;
    if (end == ranges || first < 1) fail("Invalid page range.");
    if (*end == '-') {
      const char *p = end + 1;
      last = strtol(p, &end, 10);
      if (end == p || last < first) fail("Invalid page range.");
    }
    if ((long)page >= first && (long)page <= last) return true;
    if (!*end) break;
    if (*end != ',') fail("Invalid page range.");
    ranges = end + 1;
  }
  return false;
}
static CGPDFDocumentRef open_pdf(const char *path) {
  CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL,
    (const UInt8 *)path, (CFIndex)strlen(path), false);
  if (!url) fail("Could not open PDF path.");
  CGPDFDocumentRef doc = CGPDFDocumentCreateWithURL(url);
  CFRelease(url);
  if (!doc || !CGPDFDocumentIsUnlocked(doc) || !CGPDFDocumentGetNumberOfPages(doc))
    fail("Document must be a readable, unlocked PDF.");
  return doc;
}
static unsigned char *render_page(CGPDFPageRef page, bool mono) {
  size_t bytes = (size_t)W * H * 4;
  unsigned char *rgba = calloc(1, bytes);
  CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
  CGContextRef context = CGBitmapContextCreate(rgba, W, H, 8, W * 4,
    space, kCGImageAlphaNoneSkipLast | kCGBitmapByteOrder32Big);
  CGColorSpaceRelease(space);
  if (!rgba || !context) fail("Could not render PDF page.");
  CGContextSetRGBFillColor(context, 1, 1, 1, 1);
  CGContextFillRect(context, CGRectMake(0, 0, W, H));
  /* PDF coordinates are points (1/72 inch), bitmap coordinates are pixels.
   * CGPDFPageGetDrawingTransform only scales DOWN, so passing a pixel-sized
   * rectangle leaves a 72-dpi page at its original size in a 300-dpi bitmap.
   * Establish the 300-dpi device scale first, then fit in physical points.
   */
  const CGFloat scale = 300.0 / 72.0;
  CGContextScaleCTM(context, scale, scale);
  CGRect box = CGPDFPageGetBoxRect(page, kCGPDFCropBox);
  int rotation = CGPDFPageGetRotationAngle(page) % 180;
  bool landscape = rotation == 0 ? box.size.width > box.size.height
                                 : box.size.height > box.size.width;
  CGAffineTransform transform = CGPDFPageGetDrawingTransform(page,
    kCGPDFCropBox, CGRectMake(0, 0, W / scale, H / scale), landscape ? 90 : 0, true);
  CGContextConcatCTM(context, transform);
  CGContextSetInterpolationQuality(context, kCGInterpolationHigh);
  CGContextDrawPDFPage(context, page);
  CGContextRelease(context);
  unsigned char *rgb = malloc((size_t)STRIDE * H);
  if (!rgb) fail("Not enough memory to render page.");
  for (size_t i = 0, n = (size_t)W * H; i < n; i++) {
    unsigned char *src = rgba + 4*i, *dst = rgb + 3*i;
    if (mono) {
      unsigned char gray = (unsigned char)((54u*src[0]+183u*src[1]+19u*src[2]+128u) >> 8);
      dst[0] = dst[1] = dst[2] = gray;
    } else {
      dst[0] = src[0]; dst[1] = src[1]; dst[2] = src[2];
    }
  }
  free(rgba);
  return rgb;
}
static void job_start(int fd) {
  const char *prefix =
    "\033%-12345X@PJL \n"
    "@PJL SET HOLD=OFF\n"
    "@PJL SET USERNAME=\"Mac\"\n"
    "@PJL SET JOBNAME=\"Mac print\"\n"
    "@PJL SET LOGINUSER=\"Mac\"\n"
    "@PJL JOB NAME=\"Mac print\"\n"
    "@PJL SET STRINGCODESET=UTF8\n"
    "@PJL SET PAPER=A4\n"
    "@PJL SET BORDERLESS=OFF\n"
    "@PJL SET JTTOPMARGIN=300\n"
    "@PJL SET JTBOTMARGIN=300\n"
    "@PJL SET JTLEFTMARGIN=300\n"
    "@PJL SET JTRIGHTMARGIN=300\n"
    "@PJL SET RENDERMODE=COLOR\n"
    "@PJL SET PRINTQUALITY=NORMAL\n"
    "@PJL SET DUPLEX=OFF\n"
    "@PJL SET MEDIATYPE=REGULAR\n"
    "@PJL SET MANUALFEED=OFF\n"
    "@PJL SET SOURCETRAY=AUTO\n"
    "@PJL JOBSETTINGLOG DRIVER=I0004036110000000100020009000200000007\n"
    "@PJL SET FIDELITY=TRUE\n"
    "@PJL ENTER LANGUAGE=PWGRASTER\n";
  write_all(fd, prefix, strlen(prefix));
}
static size_t convert_pdf(const char *input, int fd, const char *preview) {
  CGPDFDocumentRef doc = open_pdf(input);
  size_t page_count = CGPDFDocumentGetNumberOfPages(doc);
  int copies = preview ? 1 : copies_requested();
  const char *color = getenv("IPP_PRINT_COLOR_MODE");
  bool mono = color && (!strcmp(color, "monochrome") || !strcmp(color, "bi-level"));
  size_t selected = 0;
  for (size_t p = 1; p <= page_count; p++) if (selected_page(p)) selected++;
  if (!selected) fail("No pages in the selected range.");
  if (selected > 1000 || selected * (size_t)copies > 1000)
    fail("A single job is limited to 1000 printed pages.");
  if (!preview) job_start(fd);
  cups_raster_t *raster = preview ? NULL : cupsRasterOpen(fd, CUPS_RASTER_WRITE_PWG);
  if (!preview && !raster) fail("Could not start raster writer.");
  size_t emitted = 0;
  for (int copy = 0; copy < copies; copy++) {
    for (size_t page = 1; page <= page_count; page++) {
      if (!selected_page(page)) continue;
      if (cancelled) fail("Print job cancelled.");
      unsigned char *rgb = render_page(CGPDFDocumentGetPage(doc, page), mono);
      if (preview) {
        int out = open(preview, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (out < 0) fail("Could not create preview.");
        char header[80];
        int n = snprintf(header, sizeof(header), "P6\n%u %u\n255\n", W, H);
        write_all(out, header, (size_t)n);
        write_all(out, rgb, (size_t)STRIDE * H);
        close(out); free(rgb); CGPDFDocumentRelease(doc);
        return 1;
      }
      cups_page_header2_t h = {0};
      strcpy(h.MediaClass, "PwgRaster"); strcpy(h.MediaType, "auto");
      h.HWResolution[0] = h.HWResolution[1] = 300;
      h.PageSize[0] = 595; h.PageSize[1] = 842;
      h.cupsWidth = W; h.cupsHeight = H;
      h.cupsBitsPerColor = 8; h.cupsBitsPerPixel = 24;
      h.cupsBytesPerLine = STRIDE; h.cupsColorOrder = CUPS_ORDER_CHUNKED;
      h.cupsColorSpace = CUPS_CSPACE_SRGB; h.cupsNumColors = 3;
      if (!cupsRasterWriteHeader2(raster, &h)) fail("Could not write raster header.");
      off_t header_offset = lseek(fd, 0, SEEK_CUR) - (off_t)sizeof(h);
      const unsigned char zero[4] = {0};
      if (header_offset < 4 || pwrite(fd, zero, 4, header_offset + 480) != 4)
        fail("Could not normalize raster header.");
      for (unsigned y = 0; y < H; y++)
        if (cupsRasterWritePixels(raster, rgb + (size_t)y*STRIDE, STRIDE) != STRIDE)
          fail("Could not write raster pixels.");
      free(rgb);
      emitted++;
      fprintf(stderr, "INFO: Prepared page %zu of %zu.\n", emitted, selected * (size_t)copies);
    }
  }
  cupsRasterClose(raster);
  const char *suffix = "\033%-12345X@PJL EOJ NAME=\"Mac print\"\r\n\033%-12345X";
  write_all(fd, suffix, strlen(suffix));
  CGPDFDocumentRelease(doc);
  return emitted;
}
static bool device_available(const char *uri) {
  FILE *p = popen("/usr/libexec/cups/backend/usb", "r");
  if (!p) return false;
  char line[4096];
  bool found = false;
  while (fgets(line, sizeof(line), p)) {
    char actual[2048];
    if (sscanf(line, "direct %2047s", actual) == 1 && !strcmp(actual, uri)) found = true;
  }
  int status = pclose(p);
  return found && status == 0;
}
static void transmit(const char *path) {
  int io_lock = -1;
  const char *lock_path = getenv("BROTHER_IO_LOCK");
  if (lock_path) {
    io_lock = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (io_lock < 0) fail("Could not open printer/scanner lock.");
    time_t started = time(NULL);
    while (flock(io_lock, LOCK_EX | LOCK_NB) != 0) {
      if (errno != EWOULDBLOCK && errno != EINTR) fail("Could not lock USB device.");
      if (cancelled || time(NULL) - started > 180) fail("Printer/scanner is busy. Try again shortly.");
      usleep(100000);
    }
  }
  const char *uri = getenv("BROTHER_DEVICE_URI");
  const char *prefix = "usb://Brother/DCP-T236?serial=";
  if (!uri || strncmp(uri, prefix, strlen(prefix)))
    fail("USB printer address is missing or invalid.");
  if (!device_available(uri)) {
    fprintf(stderr, "STATE: +offline-report\n");
    fail("DCP-T236 is not connected. Turn it on, connect USB, then print again.");
  }
  fprintf(stderr, "STATE: -offline-report\nINFO: Sending document to DCP-T236 over USB.\n");
  setenv("DEVICE_URI", uri, 1);
  char *args[] = {(char *)uri, "1", "Mac", "Mac print", "1", "", (char *)path, NULL};
  posix_spawn_file_actions_t actions;
  posix_spawn_file_actions_init(&actions);
  posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
  posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0);
  pid_t pid;
  int error = posix_spawn(&pid, "/usr/libexec/cups/backend/usb", &actions, NULL, args, environ);
  posix_spawn_file_actions_destroy(&actions);
  if (error) fail("Could not start Apple's USB printing backend.");
  child = (sig_atomic_t)pid;
  int status = 0;
  time_t started = time(NULL);
  for (;;) {
    pid_t result = waitpid(pid, &status, WNOHANG);
    if (result == pid) break;
    if (result < 0 && errno != EINTR) fail("Could not wait for USB transfer.");
    if (cancelled || time(NULL) - started > 120) {
      kill(pid, SIGTERM);
      usleep(200000);
      if (waitpid(pid, &status, WNOHANG) == 0) { kill(pid, SIGKILL); waitpid(pid, &status, 0); }
      child = 0;
      fail(cancelled ? "Print job cancelled." : "USB transfer timed out. Check printer before retrying.");
    }
    usleep(100000);
  }
  child = 0;
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
    fail("USB transfer failed. Check printer before retrying.");
  if (io_lock >= 0) close(io_lock);
}
int main(int argc, char **argv) {
  atexit(cleanup);
  signal(SIGTERM, cancel_print); signal(SIGINT, cancel_print);
  if (argc == 4 && !strcmp(argv[1], "--preview")) {
    convert_pdf(argv[2], -1, argv[3]); return 0;
  }
  if (argc == 4 && !strcmp(argv[1], "--convert")) {
    int fd = open(argv[3], O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) fail("Could not create output (existing files are not overwritten).");
    convert_pdf(argv[2], fd, NULL); close(fd); return 0;
  }
  if (argc != 2) fail("Expected one PDF document.");
  const char *content = getenv("CONTENT_TYPE");
  if (content && strcmp(content, "application/pdf")) fail("This printer accepts PDF documents.");
  const char *dir = getenv("TMPDIR");
  if (!dir) dir = "/tmp";
  if (snprintf(temporary, sizeof(temporary), "%s/t236-XXXXXX", dir) >= (int)sizeof(temporary))
    fail("Temporary directory path is too long.");
  int fd = mkstemp(temporary);
  if (fd < 0) fail("Could not create temporary printer data.");
  size_t pages = convert_pdf(argv[1], fd, NULL);
  close(fd);
  if (cancelled) fail("Print job cancelled.");
  fprintf(stderr, "ATTR: job-impressions=%zu\n", pages);
  transmit(temporary);
  fprintf(stderr, "ATTR: job-impressions-completed=%zu\n", pages);
  fprintf(stderr, "STATE: none\nINFO: Document sent successfully.\n");
  return 0;
}
