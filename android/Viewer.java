package xyz.keepeda.sensors;

import android.app.Activity;
import android.os.Bundle;
import android.view.MotionEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class Viewer extends Activity implements SurfaceHolder.Callback {
    static volatile Controller controller;
    private SurfaceView surface;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        Controller c = controller;
        if (c == null) {
            TextView hint = new TextView(this);
            hint.setText("请在电脑端的校园跑控制台打开「Keep 计步窗口」。");
            setContentView(hint);
            return;
        }
        c.viewer = this;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        surface = new SurfaceView(this);
        surface.getHolder().addCallback(this);
        surface.setOnTouchListener((v, event) -> c.touch(event));
        body.addView(surface, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout bar = new LinearLayout(this);
        Button back = new Button(this);
        back.setText("返回");
        back.setOnClickListener(view -> c.back());
        bar.addView(back, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        Button close = new Button(this);
        close.setText("关闭计步窗口");
        close.setOnClickListener(view -> new android.app.AlertDialog.Builder(this)
            .setMessage("关闭将结束这个虚拟显示。请先在 Keep 保存正在进行的运动。")
            .setNegativeButton("继续使用", null).setPositiveButton("关闭", (dialog, id) -> c.requestClose()).show());
        bar.addView(close, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        body.addView(bar);
        setContentView(body);
    }

    @Override public void onBackPressed() {
        if (controller != null) controller.back();
        else super.onBackPressed();
    }
    @Override public void surfaceCreated(SurfaceHolder holder) {}
    @Override public void surfaceChanged(SurfaceHolder holder, int format, int w, int h) {
        if (controller != null && w > 0 && h > 0) controller.show(this, holder.getSurface(), w, h);
    }
    @Override public void surfaceDestroyed(SurfaceHolder holder) {
        if (controller != null) controller.detach(this);
    }
    @Override public void onDestroy() {
        if (controller != null && controller.viewer == this && isFinishing()) controller.requestClose();
        super.onDestroy();
    }
}
