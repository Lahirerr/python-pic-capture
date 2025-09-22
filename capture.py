# capture.py (robust image + video loading; auto frame capture without pause)
import os
import re
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path
from playwright.sync_api import sync_playwright

URLS_FILE = "urls.txt"
OUT_ROOT = Path("screenshots")

# --- Tunable knobs ---
DESKTOP_VIEWPORT = {"width": 1366, "height": 900}
MOBILE_DEVICE = "iPhone 12"
EXTRA_WAIT_MS = 800          # รอเพิ่มหลังจากทุกอย่างเสร็จ (ms)
IMAGES_TIMEOUT_MS = 30_000   # เวลารอรูปโหลดครบ (ms)
SCROLL_STEP = 1000           # px ต่อสเต็ป
SCROLL_PAUSE_MS = 150        # หน่วงระหว่างสเต็ป (ms)
NETWORKIDLE_TIMEOUT_MS = 20_000  # เผื่อรอ networkidle

# --- Video knobs ---
CAPTURE_VIDEO_FRAME_AT = 3     # วินาทีที่จะ capture เฟรม (ลอง 0.5–3.0)
VIDEO_READY_TIMEOUT_MS = 30_000  # ลดเวลารอวิดีโอ (15 วิ)
VIDEO_PLAY_DURATION = 0.5        # ให้วิดีโอเล่นกี่วินาทีก่อน capture
MAX_PAGE_TIMEOUT_MS = 45_000     # timeout รวมต่อ page

# ------------------------- helpers -------------------------

def pw_wait_for(page, script: str, args=None, *, timeout=None, polling="raf"):
    """
    Wrapper ป้องกันการส่ง positional arg เกินให้ page.wait_for_function
    ใช้เสมอแทนการเรียก wait_for_function โดยตรง
    """
    return page.wait_for_function(script, args, timeout=timeout, polling=polling)

def safe_name(url: str) -> str:
    p = urlparse(url)
    raw = f"{p.netloc}{p.path}".strip("/")
    if not raw:
        raw = p.netloc or "root"
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
    return raw.strip("-") or "page"

def read_urls() -> list[str]:
    f = Path(URLS_FILE)
    if f.exists():
        with f.open("r", encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    return ["https://www.example.com"]

# ------------------------- page routines -------------------------

def auto_scroll(page, step=SCROLL_STEP, pause_ms=SCROLL_PAUSE_MS):
    page.evaluate(
        """
        async ({ step, pause }) => {
          const sleep = (ms) => new Promise(r => setTimeout(r, ms));
          let total = 0;
          const maxH = () => Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight
          );
          while (total < maxH()) {
            window.scrollBy(0, step);
            total += step;
            await sleep(pause);
          }
          await sleep(pause);
          window.scrollTo(0, Math.max(0, total - step));
          await sleep(pause);
          window.scrollTo(0, 0);
        }
        """,
        {"step": step, "pause": pause_ms},
    )

def promote_lazy_images(page):
    page.evaluate(
        """
        (() => {
          const candAttrs = ["data-src", "data-original", "data-lazy-src"];
          const candSrcset = ["data-srcset", "data-lazy-srcset"];
          const imgs = Array.from(document.images);
          for (const img of imgs) {
            for (const a of candAttrs) {
              const v = img.getAttribute(a);
              if (v && !img.getAttribute("src")) { img.setAttribute("src", v); break; }
            }
            for (const s of candSrcset) {
              const v = img.getAttribute(s);
              if (v && !img.getAttribute("srcset")) { img.setAttribute("srcset", v); break; }
            }
            try { img.decode && img.decode(); } catch {}
          }
        })()
        """
    )

def wait_for_images_loaded(page, timeout_ms=IMAGES_TIMEOUT_MS):
    page.evaluate(
        """
        async (timeout) => {
          const deadline = Date.now() + timeout;
          const sleep = (ms) => new Promise(r => setTimeout(r, ms));
          while (Date.now() < deadline) {
            const imgs = Array.from(document.images);
            if (imgs.length === 0) return true;
            const ok = imgs.every(img => img.complete && Number(img.naturalWidth) > 0);
            if (ok) return true;
            await sleep(100);
          }
          throw new Error('images timeout');
        }
        """,
        timeout_ms,
    )


# ------------------------- Video helpers -------------------------

def promote_lazy_videos(page):
    page.evaluate(
        """
        (() => {
          const vids = Array.from(document.querySelectorAll('video'));
          const attrList = ['data-src', 'data-original', 'data-lazy-src'];
          for (const v of vids) {
            for (const a of attrList) {
              const val = v.getAttribute(a);
              if (val && !v.getAttribute('src')) { v.setAttribute('src', val); break; }
            }
            const sources = Array.from(v.querySelectorAll('source'));
            for (const s of sources) {
              for (const a of attrList) {
                const val = s.getAttribute(a);
                if (val && !s.getAttribute('src')) { s.setAttribute('src', val); break; }
              }
            }
            try { v.preload = 'auto'; } catch {}
          }
        })()
        """
    )

def auto_capture_video_frames(page, target_sec=CAPTURE_VIDEO_FRAME_AT, play_duration=VIDEO_PLAY_DURATION, timeout_ms=VIDEO_READY_TIMEOUT_MS):
    """
    ให้วิดีโอเล่นแล้ว capture เฟรมโดยอัตโนมัติ ไม่ต้องกด pause
    """
    try:
        page.evaluate(
            """
            async ({ target, playDuration, timeout }) => {
              const sleep = (ms) => new Promise(r => setTimeout(r, ms));
              const deadline = Date.now() + timeout;

              const vids = Array.from(document.querySelectorAll('video'));
              console.log(`Found ${vids.length} videos`);
              
              if (vids.length === 0) return true;

              const captureVideoFrame = async (v, index) => {
                try {
                  console.log(`Processing video ${index + 1}/${vids.length}`);
                  
                  v.muted = true;
                  v.playsInline = true;
                  v.loop = false;

                  // ข้าม video ที่ไม่มี src
                  if (!v.src && !v.querySelector('source')) {
                    console.log(`Video ${index + 1}: No source, skipping`);
                    return true;
                  }

                  // Timeout สำหรับ video แต่ละตัว (5 วิ)
                  const videoDeadline = Date.now() + 5000;

                  // รอ metadata พร้อม (แต่ไม่เกิน 3 วิ)
                  if (v.readyState < 1) {
                    console.log(`Video ${index + 1}: Waiting for metadata...`);
                    await Promise.race([
                      new Promise(res => v.addEventListener('loadedmetadata', res, { once: true })),
                      sleep(3000)
                    ]);
                  }

                  // ถ้าไม่มี duration ให้ข้าม
                  if (!Number.isFinite(v.duration) || v.duration <= 0) {
                    console.log(`Video ${index + 1}: Invalid duration, skipping`);
                    return true;
                  }

                  let captureTime = Math.min(target, Math.max(0, v.duration - 0.1));
                  console.log(`Video ${index + 1}: Will capture at ${captureTime}s (duration: ${v.duration}s)`);

                  try { 
                    await v.play();
                    console.log(`Video ${index + 1}: Started playing`);
                  } catch (e) {
                    console.log(`Video ${index + 1}: Play failed, continuing anyway`);
                  }

                  // รอให้เล่นถึงเวลาที่ต้องการ (แต่ไม่เกิน video timeout)
                  while (v.currentTime < captureTime && Date.now() < videoDeadline) {
                    await sleep(100);
                  }

                  console.log(`Video ${index + 1}: Current time ${v.currentTime}s, ready for capture`);

                  // รอเฟรมพร้อม
                  if ('requestVideoFrameCallback' in v) {
                    await Promise.race([
                      new Promise(res => v.requestVideoFrameCallback(() => res())),
                      sleep(500)
                    ]);
                  } else {
                    await sleep(200);
                  }

                  return true;
                  
                } catch (e) {
                  console.log(`Video ${index + 1}: Error - ${e.message}`);
                  return true; // ไม่ให้ error ทำให้ทั้ง function หยุด
                }
              };

              // จัดการ video ทีละตัว (ไม่ parallel เพื่อป้องกันค้าง)
              for (let i = 0; i < vids.length; i++) {
                if (Date.now() > deadline) {
                  console.log('Overall timeout reached, stopping video processing');
                  break;
                }
                await captureVideoFrame(vids[i], i);
              }

              console.log('Video processing completed');
              return true;
            }
            """,
            {"target": target_sec, "playDuration": play_duration, "timeout": timeout_ms},
        )
    except Exception as e:
        print(f"Video capture error: {e}")
        # ไม่ให้ throw error ต่อ เพื่อไม่ให้ script หยุด

# สำหรับกรณีที่ต้องการ capture หลายเฟรมจากวิดีโอเดียวกัน
def capture_multiple_video_frames(page, frame_times=[0.5, 1.0, 2.0], timeout_ms=VIDEO_READY_TIMEOUT_MS):
    """
    Capture หลายเฟรมจากวิดีโอในเวลาที่ต่างกัน
    """
    page.evaluate(
        """
        async ({ frameTimes, timeout }) => {
          const sleep = (ms) => new Promise(r => setTimeout(r, ms));
          const deadline = Date.now() + timeout;

          const vids = Array.from(document.querySelectorAll('video'));
          if (vids.length === 0) return true;

          for (const v of vids) {
            try { v.load?.(); } catch {}
            v.muted = true;
            v.playsInline = true;

            if (v.readyState < 1) {
              await new Promise(res => v.addEventListener('loadedmetadata', res, { once: true }));
            }

            try { await v.play(); } catch {}

            // Capture แต่ละเฟรมตามเวลาที่กำหนด
            for (const frameTime of frameTimes) {
              if (Date.now() > deadline) break;
              
              // รอให้เล่นถึงเวลาที่ต้องการ
              while (v.currentTime < frameTime && Date.now() < deadline) {
                await sleep(50);
              }

              // รอเฟรมพร้อม
              if ('requestVideoFrameCallback' in v) {
                await new Promise(res => v.requestVideoFrameCallback(() => res()));
              } else {
                await sleep(100);
              }

              console.log(`Captured frame at ${v.currentTime}s`);
            }
          }
          return true;
        }
        """,
        {"frameTimes": frame_times, "timeout": timeout_ms},
    )

# ------------------------- Orchestration -------------------------

def robust_wait(page):
    print("🔄 Loading page content...")
    
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        print("✅ Network idle")
    except Exception as e:
        print(f"⚠️  Network idle timeout: {e}")

    print("🔄 Auto scrolling...")
    auto_scroll(page)
    
    print("🔄 Promoting lazy images...")
    promote_lazy_images(page)

    print("🔄 Promoting lazy videos...")
    promote_lazy_videos(page)
    
    print("🔄 Processing videos for capture...")
    try:
        auto_capture_video_frames(page, 
                                target_sec=CAPTURE_VIDEO_FRAME_AT, 
                                play_duration=VIDEO_PLAY_DURATION,
                                timeout_ms=VIDEO_READY_TIMEOUT_MS)
        print("✅ Video frames processed")
    except Exception as e:
        print(f"⚠️  Video processing error: {e}")

    print("🔄 Waiting for images to load...")
    try:
        wait_for_images_loaded(page)
        print("✅ Images loaded")
    except Exception as e:
        print(f"⚠️  Image loading timeout: {e}")
    
    print("🔄 Final wait...")
    page.wait_for_timeout(EXTRA_WAIT_MS)
    print("✅ Page ready for screenshot")

# ------------------------- Main -------------------------

def capture_all(urls: list[str]) -> None:
    date_folder = datetime.now().strftime("%Y-%m-%d_%H-%M")
    out_dir = OUT_ROOT / date_folder
    (out_dir / "desktop").mkdir(parents=True, exist_ok=True)
    (out_dir / "mobile").mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # Desktop
        desktop_browser = p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-web-security",
                "--allow-running-insecure-content"
            ],
        )
        desktop_ctx = desktop_browser.new_context(
            viewport=DESKTOP_VIEWPORT,
            device_scale_factor=2,
        )

        # Mobile
        iphone = p.devices[MOBILE_DEVICE]
        mobile_browser = p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-web-security",
                "--allow-running-insecure-content"
            ],
        )
        mobile_ctx = mobile_browser.new_context(**iphone)

        for url in urls:
            name = safe_name(url)
            print(f"\n🌐 Processing: {url}")
            
            try:
                # Desktop
                print("  📱 Desktop capture...")
                d = desktop_ctx.new_page()
                
                # เพิ่ม timeout สำหรับ page
                d.set_default_timeout(MAX_PAGE_TIMEOUT_MS)
                
                d.add_init_script("""
                  (() => {
                    const patch = (v) => { 
                      try { 
                        v.muted = true; 
                        v.playsInline = true;
                        v.preload = 'auto';
                        // Force play เพื่อให้ได้เฟรมที่ดี
                        v.addEventListener('loadeddata', () => {
                          v.play().catch(() => {});
                        });
                      } catch {} 
                    };
                    
                    // Patch existing videos
                    document.querySelectorAll('video').forEach(patch);
                    
                    // Patch new videos
                    new MutationObserver(() => {
                      document.querySelectorAll('video').forEach(patch);
                    }).observe(document.documentElement, { childList: true, subtree: true });
                  })();
                """)
                
                try:
                    d.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    robust_wait(d)
                    d_out = out_dir / "desktop" / f"{name}.png"
                    d.screenshot(path=str(d_out), full_page=True)
                    print("    ✅ Desktop done")
                except Exception as e:
                    print(f"    ❌ Desktop failed: {e}")
                finally:
                    d.close()

                # Mobile
                print("  📱 Mobile capture...")
                m = mobile_ctx.new_page()
                m.set_default_timeout(MAX_PAGE_TIMEOUT_MS)
                
                m.add_init_script("""
                  (() => {
                    const patch = (v) => { 
                      try { 
                        v.muted = true; 
                        v.playsInline = true;
                        v.preload = 'auto';
                        v.addEventListener('loadeddata', () => {
                          v.play().catch(() => {});
                        });
                      } catch {} 
                    };
                    document.querySelectorAll('video').forEach(patch);
                    new MutationObserver(() => {
                      document.querySelectorAll('video').forEach(patch);
                    }).observe(document.documentElement, { childList: true, subtree: true });
                  })();
                """)
                
                try:
                    m.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    robust_wait(m)
                    m_out = out_dir / "mobile" / f"{name}.png"
                    m.screenshot(path=str(m_out), full_page=True)
                    print("    ✅ Mobile done")
                except Exception as e:
                    print(f"    ❌ Mobile failed: {e}")
                finally:
                    m.close()

                print(f"✅ Completed: {url}")
                
            except Exception as e:
                print(f"❌ Overall failed: {url} -> {e}")

        desktop_ctx.close(); desktop_browser.close()
        mobile_ctx.close(); mobile_browser.close()

if __name__ == "__main__":
    urls = read_urls()
    if not urls:
        print("No URLs to capture. Add some lines into urls.txt")
        raise SystemExit(1)
    capture_all(urls)
    print("\nDone. Check the 'screenshots/' folder.")