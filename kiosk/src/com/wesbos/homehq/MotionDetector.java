package com.wesbos.homehq;

import android.graphics.SurfaceTexture;
import android.hardware.Camera;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import java.util.List;

/**
 * Camera motion detection using the old android.hardware.Camera API, so it works on Android 5.
 *
 * Opens the front camera (the back one if there is no front camera) at its smallest preview size
 * and, a few times a second, averages the brightness (Y plane of the NV21 frame) into a 16x12 grid.
 * A cell "changed" when its brightness moved more than CELL_DELTA from the previous sample, after
 * removing the frame-wide average change (auto-exposure or a light switching on shifts every cell
 * together, which is not motion). Enough changed cells = motion.
 *
 * No preview is shown: frames go to an off-screen SurfaceTexture. All camera work happens on its
 * own thread; the listener is called on the main thread, at most once per COOLDOWN_MS.
 */
class MotionDetector {
  interface Listener {
    void onMotion();
    /** Auto-exposure is now locked; meanY is the frame brightness (0-255) at that moment. */
    default void onExposureLocked(float meanY) {}
    /** Average frame brightness (0-255), every LIGHT_MS, once exposure is locked. */
    default void onLight(float meanY) {}
  }

  // Light metering: after the camera settles, auto-exposure is locked so frame brightness follows
  // the room's light instead of being normalised by the camera.
  static final long SETTLE_MS = 3000;
  static final long LIGHT_MS = 2000;
  private boolean exposureLocked;
  private long openedAt, lastLight;

  static final int GRID_W = 16, GRID_H = 12;
  static final int CELL_DELTA = 14;          // brightness change (0-255) for a cell to count
  static final long SAMPLE_MS = 300;         // compare frames about 3x a second
  static final long COOLDOWN_MS = 1500;

  private final Listener listener;
  private final Handler main = new Handler(Looper.getMainLooper());
  private final int minCells;
  private HandlerThread thread;
  private Handler camHandler;
  private Camera camera;
  private SurfaceTexture texture;
  private int width, height;
  private int[] prev;
  private long lastSample, lastMotion;

  /** sensitivity 1-100: higher reacts to smaller movement (fewer changed cells needed). */
  MotionDetector(int sensitivity, Listener listener) {
    this.listener = listener;
    int s = Math.max(1, Math.min(100, sensitivity));
    // 100 -> 1 cell, 50 -> 10 cells, 1 -> 20 cells (of 192)
    this.minCells = Math.max(1, Math.round(20 - (s - 1) * 19f / 99f));
  }

  static boolean hasCamera() {
    return Camera.getNumberOfCameras() > 0;
  }

  void start() {
    if (thread != null) return;
    thread = new HandlerThread("motion");
    thread.start();
    camHandler = new Handler(thread.getLooper());
    camHandler.post(this::open);
  }

  void stop() {
    if (thread == null) return;
    final HandlerThread t = thread;
    camHandler.post(() -> {
      close();
      t.quit();
    });
    thread = null;
    camHandler = null;
  }

  private void open() {
    try {
      int id = 0;
      Camera.CameraInfo info = new Camera.CameraInfo();
      for (int i = 0; i < Camera.getNumberOfCameras(); i++) {
        Camera.getCameraInfo(i, info);
        if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) { id = i; break; }
      }
      camera = Camera.open(id);
      Camera.Parameters p = camera.getParameters();
      Camera.Size best = null;
      List<Camera.Size> sizes = p.getSupportedPreviewSizes();
      for (Camera.Size s : sizes) {
        if (s.width < 160) continue;
        if (best == null || s.width * s.height < best.width * best.height) best = s;
      }
      if (best == null) best = sizes.get(0);
      p.setPreviewSize(best.width, best.height);
      camera.setParameters(p);
      width = best.width;
      height = best.height;
      texture = new SurfaceTexture(10);
      camera.setPreviewTexture(texture);
      int bufSize = width * height * 3 / 2; // NV21
      camera.addCallbackBuffer(new byte[bufSize]);
      camera.addCallbackBuffer(new byte[bufSize]);
      camera.setPreviewCallbackWithBuffer((data, cam) -> {
        try {
          analyse(data);
        } finally {
          if (camera != null) camera.addCallbackBuffer(data);
        }
      });
      camera.startPreview();
      openedAt = SystemClock.elapsedRealtime();
      exposureLocked = false;
      Log.i(MainActivity.TAG, "Motion detection on (camera " + id + ", " + width + "x" + height
          + ", " + minCells + " cells)");
    } catch (Exception e) {
      Log.w(MainActivity.TAG, "Motion detection unavailable: " + e);
      close();
    }
  }

  private void lightTick(long now, final float mean) {
    if (!exposureLocked) {
      if (now - openedAt < SETTLE_MS || camera == null) return;
      try {
        Camera.Parameters p = camera.getParameters();
        if (!p.isAutoExposureLockSupported()) return;
        p.setAutoExposureLock(true);
        camera.setParameters(p);
        exposureLocked = true;
        lastLight = now;
        Log.i(MainActivity.TAG, "Exposure locked at brightness " + Math.round(mean));
        main.post(() -> listener.onExposureLocked(mean));
      } catch (Exception e) {
        Log.w(MainActivity.TAG, "Could not lock exposure: " + e);
        openedAt = now; // try again later
      }
      return;
    }
    if (now - lastLight >= LIGHT_MS) {
      lastLight = now;
      main.post(() -> listener.onLight(mean));
    }
  }

  private void close() {
    if (camera != null) {
      try {
        camera.setPreviewCallbackWithBuffer(null);
        camera.stopPreview();
      } catch (Exception ignored) {
      }
      camera.release();
      camera = null;
    }
    if (texture != null) {
      texture.release();
      texture = null;
    }
    prev = null;
  }

  private void analyse(byte[] y) {
    long now = SystemClock.elapsedRealtime();
    if (now - lastSample < SAMPLE_MS) return;
    lastSample = now;

    int[] grid = new int[GRID_W * GRID_H];
    int cw = width / GRID_W, ch = height / GRID_H;
    // Sample every 4th pixel in each direction; plenty for a brightness average.
    for (int gy = 0; gy < GRID_H; gy++) {
      for (int gx = 0; gx < GRID_W; gx++) {
        int sum = 0, n = 0;
        for (int py = gy * ch; py < (gy + 1) * ch; py += 4) {
          int row = py * width;
          for (int px = gx * cw; px < (gx + 1) * cw; px += 4) {
            sum += y[row + px] & 0xFF;
            n++;
          }
        }
        grid[gy * GRID_W + gx] = n == 0 ? 0 : sum / n;
      }
    }
    int[] last = prev;
    prev = grid;
    if (last == null) return;

    long sum = 0;
    for (int v : grid) sum += v;
    float mean = sum / (float) grid.length;
    lightTick(now, mean);

    int total = 0;
    for (int i = 0; i < grid.length; i++) total += grid[i] - last[i];
    int shift = total / grid.length; // frame-wide change: exposure / room light, not motion
    // With exposure locked, a dim room gives a dark, low-contrast image: lower the threshold
    // with the image brightness so motion still registers (but never below 5).
    int delta = Math.max(5, Math.min(CELL_DELTA, Math.round(mean * 0.2f)));
    int changed = 0;
    for (int i = 0; i < grid.length; i++) {
      if (Math.abs(grid[i] - last[i] - shift) > delta) changed++;
    }
    if (changed >= minCells && now - lastMotion > COOLDOWN_MS) {
      lastMotion = now;
      main.post(listener::onMotion);
    }
  }
}
