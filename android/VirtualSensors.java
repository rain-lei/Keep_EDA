package xyz.keepeda.sensors;

import android.content.Context;
import android.hardware.Sensor;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.os.SystemClock;
import android.view.Surface;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executor;

/** Android 14's virtual-device System APIs, which aren't in the public SDK jar. */
final class VirtualSensors implements AutoCloseable {
    private static final String PREFIX = "android.companion.virtual.";
    private final Object device;
    private final Map<Integer, Object> sensors = new HashMap<>();
    private final Constructor<?> eventBuilder;
    private final Method timestamp, buildEvent, sendEvent;
    private final Class<?> deviceType;

    VirtualSensors(Context context, int association) throws Exception {
        Class<?> paramsType = Class.forName(PREFIX + "VirtualDeviceParams");
        Class<?> configType = Class.forName(PREFIX + "sensor.VirtualSensorConfig");
        Class<?> callbackType = Class.forName(PREFIX + "sensor.VirtualSensorCallback");
        Object params = Class.forName(paramsType.getName() + "$Builder").getConstructor().newInstance();
        Class<?> builder = params.getClass();
        builder.getMethod("setDevicePolicy", int.class, int.class).invoke(params, 0, 1); // SENSORS, CUSTOM
        Object callback = Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{callbackType},
            (proxy, method, args) -> null);
        Executor direct = Runnable::run;
        builder.getMethod("setVirtualSensorCallback", Executor.class, callbackType).invoke(params, direct, callback);
        Class<?> config = Class.forName(configType.getName() + "$Builder");
        for (int type : new int[]{Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_STEP_DETECTOR, Sensor.TYPE_STEP_COUNTER}) {
            String name = type == Sensor.TYPE_ACCELEROMETER ? "Accelerometer"
                          : type == Sensor.TYPE_STEP_COUNTER ? "Step Counter" : "Step Detector";
            Object sensor = config.getConstructor(int.class, String.class).newInstance(type, "Keep EDA " + name);
            config.getMethod("setVendor", String.class).invoke(sensor, "Keep EDA");
            config.getMethod("setMinDelay", int.class).invoke(sensor, type == Sensor.TYPE_ACCELEROMETER ? 10000 : 0);
            config.getMethod("setMaxDelay", int.class).invoke(sensor, 200000);
            config.getMethod("setMaximumRange", float.class).invoke(sensor, type == Sensor.TYPE_ACCELEROMETER ? 40f : 16777216f);
            config.getMethod("setResolution", float.class).invoke(sensor, type == Sensor.TYPE_ACCELEROMETER ? .01f : 1f);
            builder.getMethod("addVirtualSensorConfig", configType).invoke(params, config.getMethod("build").invoke(sensor));
        }
        Class<?> eb = Class.forName(PREFIX + "sensor.VirtualSensorEvent$Builder");
        eventBuilder = eb.getConstructor(float[].class);
        timestamp = eb.getMethod("setTimestampNanos", long.class);
        buildEvent = eb.getMethod("build");
        Class<?> sensorType = Class.forName(PREFIX + "sensor.VirtualSensor");
        sendEvent = sensorType.getMethod("sendEvent", Class.forName(PREFIX + "sensor.VirtualSensorEvent"));
        Object manager = context.getSystemService("virtualdevice");
        if (manager == null) throw new IllegalStateException("Android 缺少虚拟设备服务");
        device = manager.getClass().getMethod("createVirtualDevice", int.class, paramsType)
                        .invoke(manager, association, builder.getMethod("build").invoke(params));
        deviceType = device.getClass();
        try {
            for (Object sensor : (List<?>) deviceType.getMethod("getVirtualSensorList").invoke(device)) {
                sensors.put((Integer) sensorType.getMethod("getType").invoke(sensor), sensor);
            }
            if (sensors.size() != 3) throw new IllegalStateException("Android 未创建全部跑步传感器");
        } catch (Exception error) {
            close();
            throw error;
        }
    }

    VirtualDisplay display(int width, int height, int density, Surface surface) throws Exception {
        Method method = deviceType.getMethod("createVirtualDisplay", int.class, int.class, int.class,
                Surface.class, int.class, Executor.class, VirtualDisplay.Callback.class);
        int trusted = DisplayManager.class.getField("VIRTUAL_DISPLAY_FLAG_TRUSTED").getInt(null);
        return (VirtualDisplay) method.invoke(device, width, height, density, surface,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY | trusted, null, null);
    }

    void send(int type, float... values) throws Exception {
        Object builder = eventBuilder.newInstance((Object) values);
        timestamp.invoke(builder, SystemClock.elapsedRealtimeNanos());
        sendEvent.invoke(sensors.get(type), buildEvent.invoke(builder));
    }

    void counter(long count) throws Exception {
        // Android 14 copies float array storage into sensors_event_t. The step
        // counter is the uint64 member of that union, including its high word.
        send(Sensor.TYPE_STEP_COUNTER, Float.intBitsToFloat((int) count), Float.intBitsToFloat((int)(count >>> 32)));
    }

    @Override public void close() throws Exception {
        ((AutoCloseable) device).close();
    }
}
