package xyz.keepeda.sensors;

/** One compass attitude for all sensor consumers. North=0, clockwise positive. */
final class Heading {
    // Keep the portrait device tilted up 60 degrees: a fully upright phone makes
    // the accelerometer/magnetometer azimuth singular. All sensor axes agree.
    private static final double TILT = Math.PI / 3;
    private static final double S = Math.sin(TILT), C = Math.cos(TILT);
    private double degrees, target;
    private long lastNanos;
    private boolean initialized;

    static double normalize(double value) { return (value % 360 + 360) % 360; }

    void set(double value, boolean immediate) {
        if (!Double.isFinite(value)) throw new IllegalArgumentException("heading");
        target = normalize(value);
        if (immediate || !initialized) degrees = target;
        initialized = true;
    }

    void tick(long now) {
        double dt = lastNanos == 0 ? 0 : Math.min(.1, Math.max(0, (now-lastNanos)*1e-9));
        lastNanos = now;
        double delta = normalize(target-degrees+180)-180;
        double step = delta*(1-Math.exp(-dt/.18));
        double limit = 180*dt;
        degrees = normalize(degrees + Math.max(-limit, Math.min(limit, step)));
    }

    float azimuth() { return (float) degrees; }

    float[] acceleration(double sideways, double vertical, double forward) {
        return new float[]{(float)sideways, (float)(vertical*S+forward*C),
                           (float)(vertical*C-forward*S)};
    }

    float[] gravity() { return acceleration(0, 9.81, 0); }

    float[] gait(double foot, double length) {
        double angle = 2*Math.PI*(foot-.5);
        double impact = Math.min(6, Math.max(3.6, 4.8*Math.pow(length,.35)));
        double gain = Math.min(1, foot);
        // Keep's compass combines raw acceleration and magnetic field instead
        // of using the fused rotation vector. Limit horizontal sway so footfall
        // translation is not mistaken for yaw; retain the vertical landing pulse.
        return acceleration(gain*.08*Math.sin(Math.PI*foot),
            9.81 + gain*impact*(Math.cos(angle)+.24*Math.cos(2*angle)),
            gain*(.10*Math.sin(angle)-.025*Math.sin(2*angle)));
    }

    float[] magnetic() {
        // Synthetic field: 30 microtesla north, 40 down, in device coordinates.
        double h = Math.toRadians(degrees);
        return new float[]{(float)(-30*Math.sin(h)), (float)(30*Math.cos(h)*C-40*S),
                           (float)(-30*Math.cos(h)*S-40*C)};
    }

    float[] rotation() {
        // Device-to-world quaternion Rz(-heading) * Rx(tilt). The optional fifth
        // value is estimated heading accuracy in radians, not another angle.
        double half = Math.toRadians(degrees)/2;
        double sh = Math.sin(half), ch = Math.cos(half);
        double st = Math.sin(TILT/2), ct = Math.cos(TILT/2);
        return new float[]{(float)(ch*st), (float)(-sh*st), (float)(-sh*ct),
                           (float)(ch*ct), .01f};
    }

    float[] orientation() { return new float[]{(float)degrees, -60f, 0f}; }
}
