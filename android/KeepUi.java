package xyz.keepeda.sensors;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.os.SystemClock;
import android.util.SparseArray;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;

/** Observe only Keep on our virtual display; never save or discard an activity. */
final class KeepUi implements AutoCloseable {
    private final Controller owner;
    private final UiAutomation automation;
    private final ScheduledExecutorService worker = Executors.newSingleThreadScheduledExecutor();
    private volatile String state = "UNKNOWN";
    private volatile long observed, lastQuery;
    private final AtomicReference<String> requested = new AtomicReference<>();

    KeepUi(Controller owner, UiAutomation automation) {
        this.owner = owner;
        this.automation = automation;
        AccessibilityServiceInfo info = automation.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
        automation.setServiceInfo(info);
        worker.scheduleWithFixedDelay(this::scan, 0, 250, TimeUnit.MILLISECONDS);
    }

    String state() {
        lastQuery = SystemClock.elapsedRealtime();
        return lastQuery-observed < 1800 ? state : "UNKNOWN";
    }
    void request(String action) {
        if (!action.equals("PAUSE") && !action.equals("RESUME")) throw new IllegalArgumentException("action");
        requested.set(action);
        lastQuery = SystemClock.elapsedRealtime();
    }

    private String role(AccessibilityNodeInfo node) {
        if (!node.isVisibleToUser() || !node.isEnabled()) return "";
        String id = node.getViewIdResourceName();
        if ("com.gotokeep.keep:id/btnPause".equals(id) && node.isClickable()) return "PAUSE";
        if ("com.gotokeep.keep:id/btnResume".equals(id) && node.isClickable()) return "RESUME";
        if ("com.gotokeep.keep:id/layoutGo".equals(id) && node.isClickable()) return "GO";
        return "";
    }

    private void collect(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> nodes) {
        nodes.add(node);
        if (nodes.size() >= 600) return;
        for (int i=0; i<node.getChildCount() && nodes.size()<600; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) collect(child, nodes);
        }
    }

    private void scan() {
        if (SystemClock.elapsedRealtime()-lastQuery > 2000) return;
        List<AccessibilityNodeInfo> nodes = new ArrayList<>();
        try {
            SparseArray<List<AccessibilityWindowInfo>> displays = automation.getWindowsOnAllDisplays();
            List<AccessibilityWindowInfo> windows = displays.get(owner.displayId());
            if (windows != null) {
                // Only the top visible Keep window: a dialog must not expose a covered run control.
                for (AccessibilityWindowInfo window : windows) {
                    AccessibilityNodeInfo root = window.getRoot();
                    if (root == null) continue;
                    if ("com.gotokeep.keep".contentEquals(String.valueOf(root.getPackageName()))) {
                        collect(root, nodes);
                        break;
                    }
                    root.recycle();
                }
            }
            AccessibilityNodeInfo pause = null, resume = null;
            boolean ready = false;
            for (AccessibilityNodeInfo node : nodes) {
                String role = role(node);
                if (role.equals("PAUSE")) pause = node;
                if (role.equals("RESUME")) resume = node;
                if (role.equals("GO")) ready = true;
            }
            state = resume != null ? "PAUSED" : pause != null ? "RUNNING" : ready ? "READY" : "UNKNOWN";
            observed = SystemClock.elapsedRealtime();
            String action = requested.getAndSet(null);
            AccessibilityNodeInfo target = "PAUSE".equals(action) ? pause : "RESUME".equals(action) ? resume : null;
            if (target != null) target.performAction(AccessibilityNodeInfo.ACTION_CLICK);
        } catch (Exception ignored) {
            state = "UNKNOWN";
        } finally {
            for (AccessibilityNodeInfo node : nodes) node.recycle();
        }
    }

    @Override public void close() { worker.shutdownNow(); }
}
