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

/** Observe only Keep on our virtual display; never save or discard an activity. */
final class KeepUi implements AutoCloseable {
    private final Controller owner;
    private final UiAutomation automation;
    private final ScheduledExecutorService worker = Executors.newSingleThreadScheduledExecutor();
    private volatile String state = "UNKNOWN";
    private volatile long observed, lastQuery;

    KeepUi(Controller owner, UiAutomation automation) {
        this.owner = owner;
        this.automation = automation;
        AccessibilityServiceInfo info = automation.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
        automation.setServiceInfo(info);
        worker.scheduleWithFixedDelay(() -> {
            if (SystemClock.elapsedRealtime() - lastQuery <= 2000) scan(null);
        }, 0, 250, TimeUnit.MILLISECONDS);
    }

    String state() {
        lastQuery = SystemClock.elapsedRealtime();
        return lastQuery-observed < 1800 ? state : "UNKNOWN";
    }
    String request(String action) {
        if (!action.equals("SELECT_RUN") && !action.equals("GO") && !action.equals("PAUSE") && !action.equals("RESUME"))
            throw new IllegalArgumentException("action");
        lastQuery = SystemClock.elapsedRealtime();
        return scan(action);
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

    private synchronized String scan(String action) {
        List<AccessibilityNodeInfo> nodes = new ArrayList<>();
        try {
            SparseArray<List<AccessibilityWindowInfo>> displays = automation.getWindowsOnAllDisplays();
            List<AccessibilityWindowInfo> windows = displays.get(owner.displayId());
            if (windows != null) {
                // Only the top visible Keep window: a dialog must not expose a covered run control.
                for (AccessibilityWindowInfo window : windows) {
                    if (window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                    AccessibilityNodeInfo root = window.getRoot();
                    if (root == null) break;
                    if ("com.gotokeep.keep".contentEquals(String.valueOf(root.getPackageName()))) {
                        collect(root, nodes);
                    } else {
                        root.recycle();
                    }
                    break;
                }
            }
            AccessibilityNodeInfo pause = null, resume = null, go = null, outdoorLabel = null;
            boolean locationPrompt = false;
            for (AccessibilityNodeInfo node : nodes) {
                String role = role(node);
                if (role.equals("PAUSE")) pause = node;
                if (role.equals("RESUME")) resume = node;
                if (role.equals("GO")) go = node;
                if ("com.gotokeep.keep:id/textGameName".equals(node.getViewIdResourceName()) &&
                    "户外跑步".contentEquals(String.valueOf(node.getText())) && node.isVisibleToUser()) outdoorLabel = node;
                if (node.isVisibleToUser() && ("精准运动定位".contentEquals(String.valueOf(node.getText())) ||
                        "精准运动定位".contentEquals(String.valueOf(node.getContentDescription())))) locationPrompt = true;
            }
            state = locationPrompt ? "LOCATION_PROMPT" :
                    resume != null ? "PAUSED" : pause != null ? "RUNNING" : go != null ? "READY" : "UNKNOWN";
            observed = SystemClock.elapsedRealtime();
            if (action == null) return state;
            if ("SELECT_RUN".equals(action)) {
                if (go == null || outdoorLabel == null || !"READY".equals(state)) return "MISSING";
                AccessibilityNodeInfo tile = outdoorLabel.getParent();
                try {
                    return tile != null && tile.isVisibleToUser() && tile.isEnabled() && tile.isClickable() &&
                           tile.performAction(AccessibilityNodeInfo.ACTION_CLICK) ? "CLICKED" : "REJECTED";
                } finally {
                    if (tile != null) tile.recycle();
                }
            }
            if (("PAUSE".equals(action) && "PAUSED".equals(state)) ||
                ("RESUME".equals(action) && "RUNNING".equals(state))) return "ALREADY";
            AccessibilityNodeInfo target = "PAUSE".equals(action) && "RUNNING".equals(state) ? pause :
                "RESUME".equals(action) && "PAUSED".equals(state) ? resume :
                "GO".equals(action) && "READY".equals(state) ? go : null;
            if (target == null) return "MISSING";
            return target.performAction(AccessibilityNodeInfo.ACTION_CLICK) ? "CLICKED" : "REJECTED";
        } catch (Exception ignored) {
            state = "UNKNOWN";
            return "MISSING";
        } finally {
            for (AccessibilityNodeInfo node : nodes) node.recycle();
        }
    }

    @Override public void close() { worker.shutdownNow(); }
}
