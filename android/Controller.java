package xyz.keepeda.sensors;

import android.app.Activity;
import android.app.ActivityOptions;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.companion.CompanionDeviceManager;
import android.content.Context;
import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.display.VirtualDisplay;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Log;
import android.view.InputEvent;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Surface;
import java.io.*;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;

public final class Controller extends Instrumentation {
    private final CountDownLatch closing = new CountDownLatch(1);
    private final ScheduledExecutorService clock = Executors.newSingleThreadScheduledExecutor();
    private volatile LocalServerSocket server;
    private volatile LocalSocket client;
    private volatile int displayId = -1;
    volatile Viewer viewer;
    private UiAutomation automation;
    private KeepUi keepUi;
    private Method setDisplayId;
    private VirtualSensors sensors;
    private VirtualDisplay display;
    private int association;
    private String failure;
    private boolean running;
    private long total, base, lastFrame, lastIdle;
    private int limit;
    private double phase, cadence, length;
    private final Heading heading = new Heading();

    @Override public void onCreate(Bundle args) {
        super.onCreate(args);
        association = Integer.parseInt(args.getString("association"));
        start();
    }

    @Override public void onStart() {
        Context app = getTargetContext();
        Bundle result = new Bundle();
        try {
            automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            automation.adoptShellPermissionIdentity("android.permission.CREATE_VIRTUAL_DEVICE",
                "android.permission.ADD_TRUSTED_DISPLAY", "android.permission.INTERNAL_SYSTEM_WINDOW",
                "android.permission.INJECT_EVENTS");
            setDisplayId = InputEvent.class.getMethod("setDisplayId", int.class);
            sensors = new VirtualSensors(app, association);
            keepUi = new KeepUi(this, automation);
            server = new LocalServerSocket("xyz.keepeda.sensors.v1");
            Viewer.controller = this;
            app.startActivity(new Intent(app, Viewer.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            new Thread(this::listen, "eda-adb").start();
            clock.scheduleAtFixedRate(() -> {
                try { tick(); }
                catch (Exception error) { fail(error); }
            }, 0, 20, TimeUnit.MILLISECONDS);
            closing.await();
        } catch (Exception error) {
            fail(error);
        } finally {
            Viewer.controller = null;
            if (keepUi != null) keepUi.close();
            clock.shutdownNow();
            try { clock.awaitTermination(1, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
            try { if (server != null) server.close(); } catch (IOException ignored) {}
            try { if (client != null) client.close(); } catch (IOException ignored) {}
            if (display != null || viewer != null) runOnMainSync(() -> {
                if (display != null) display.release();
                if (viewer != null) viewer.finish();
            });
            try { if (sensors != null) sensors.close(); } catch (Exception error) { Log.w("KeepEDA", "close", error); }
            try { app.getSystemService(CompanionDeviceManager.class).disassociate(association); }
            catch (RuntimeException error) { Log.w("KeepEDA", "association", error); }
            if (automation != null) automation.dropShellPermissionIdentity();
            if (failure != null) result.putString("error", failure);
            finish(failure == null ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
        }
    }

    void show(Viewer window, Surface surface, int width, int height) {
        try {
            int density = window.getResources().getDisplayMetrics().densityDpi;
            if (display != null) {
                display.resize(width, height, density);
                display.setSurface(surface);
                return;
            }
            display = sensors.display(width, height, density, surface);
            int id = display.getDisplay().getDisplayId();
            Intent keep = window.getPackageManager().getLaunchIntentForPackage("com.gotokeep.keep");
            if (keep == null) throw new IllegalStateException("此实例未安装 Keep");
            window.startActivity(keep, ActivityOptions.makeBasic().setLaunchDisplayId(id).toBundle());
            displayId = id;
        } catch (Exception error) { fail(error); }
    }

    void detach(Viewer window) {
        if (viewer == window && display != null) display.setSurface(null);
    }

    boolean touch(MotionEvent original) {
        MotionEvent copy = MotionEvent.obtain(original);
        try { return inject(copy); } finally { copy.recycle(); }
    }

    void back() {
        long now = SystemClock.uptimeMillis();
        inject(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_BACK, 0));
        inject(new KeyEvent(now, now, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_BACK, 0));
    }

    private boolean inject(InputEvent event) {
        if (displayId <= 0 || closing.getCount() == 0) return false;
        try {
            setDisplayId.invoke(event, displayId);
            return automation.injectInputEvent(event, false);
        } catch (Exception error) { Log.w("KeepEDA", "input", error); return false; }
    }

    void requestClose() { closing.countDown(); }
    int displayId() { return displayId; }
    private void fail(Exception error) {
        Throwable cause = error;
        while (cause.getCause() != null) cause = cause.getCause();
        failure = cause.toString();
        Log.e("KeepEDA", failure, error);
        requestClose();
    }

    private void listen() {
        try {
            while (closing.getCount() != 0) {
                try (LocalSocket socket = server.accept()) {
                    Credentials peer = socket.getPeerCredentials();
                    if (peer.getUid() != 2000 && peer.getUid() != 0) continue;
                    client = socket;
                    BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.US_ASCII));
                    PrintWriter out = new PrintWriter(socket.getOutputStream(), true);
                    out.println("EDA4 " + displayId);
                    for (String line; (line = in.readLine()) != null;) {
                        if (line.length() > 180) break;
                        if (line.equals("CLOSE")) { requestClose(); break; }
                        if (line.equals("STATE")) { out.println(keepUi.state()); continue; }
                        if (line.startsWith("ACTION ")) { out.println(keepUi.request(line.substring(7))); continue; }
                        if (command(line)) out.println("OK");
                    }
                } catch (IOException error) {
                    if (closing.getCount() != 0) Log.w("KeepEDA", "ADB disconnected", error);
                } catch (IllegalArgumentException error) {
                    Log.w("KeepEDA", "invalid control message", error);
                } finally {
                    synchronized (this) { running = false; }
                    client = null;
                }
            }
        } catch (Exception error) {
            if (closing.getCount() != 0) fail(error);
        }
    }

    private synchronized boolean command(String line) throws Exception {
        String[] fields = line.split(" ");
        switch (fields[0]) {
            case "HEADING":
                if (fields.length != 3 || !(fields[2].equals("0") || fields[2].equals("1")))
                    throw new IllegalArgumentException("heading");
                heading.set(Double.parseDouble(fields[1]), fields[2].equals("1"));
                return true;
            case "BEGIN":
                limit = Integer.parseInt(fields[1]);
                if (limit < 0 || limit > 300000) throw new IllegalArgumentException("steps");
                base = total; phase = 0; running = false;
                sensors.counter(total);
                return true;
            case "S":
                phase = Double.parseDouble(fields[1]);
                cadence = Double.parseDouble(fields[2]);
                length = Double.parseDouble(fields[3]);
                if (!(phase >= 0 && phase <= limit+1 && cadence > 0 && cadence < 300 && length > 0 && length < 8))
                    throw new IllegalArgumentException("gait");
                lastFrame = SystemClock.elapsedRealtimeNanos(); running = true;
                return false;
            case "REST":
                running = false;
                sensors.send(Sensor.TYPE_ACCELEROMETER, heading.gravity());
                return true;
            case "END":
                running = false;
                stepsTo(Integer.parseInt(fields[1]));
                return true;
            default: throw new IllegalArgumentException("command");
        }
    }

    private void stepsTo(int target) throws Exception {
        long expected = base + Math.min(limit, Math.max(0, target));
        if (expected <= total) return;
        // A stalled host may skip several beats. Publish the absolute count
        // without sending a burst of obsolete detector events.
        if (expected-total <= 3) {
            for (long i = total; i < expected; i++) sensors.send(Sensor.TYPE_STEP_DETECTOR, 1);
        }
        total = expected;
        sensors.counter(total);
    }

    private synchronized void tick() throws Exception {
        long now = SystemClock.elapsedRealtimeNanos();
        heading.tick(now);
        sensors.orient(heading);
        double age = (now-lastFrame)*1e-9;
        if (!running || age > .28) {
            if (now-lastIdle > 400000000L) {
                sensors.send(Sensor.TYPE_ACCELEROMETER, heading.gravity());
                sensors.counter(total);
                lastIdle = now;
            }
            return;
        }
        double foot = Math.min(limit + .49, phase + age*cadence/60);
        sensors.send(Sensor.TYPE_ACCELEROMETER, heading.gait(foot, length));
        stepsTo((int)Math.floor(foot+.5));
    }
}
