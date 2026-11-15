package xyz.keepeda.sensors;

/** Independent reconstruction catches quaternion signs, swapped axes and north wrap. */
public final class HeadingTest {
    private static void close(double actual, double expected, double tolerance) {
        if (Math.abs(actual-expected) > tolerance)
            throw new AssertionError(actual + " != " + expected);
    }
    private static double difference(double a, double b) { return Heading.normalize(a-b+180)-180; }
    private static double dot(float[] a, float[] b) {
        return a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
    }
    private static double magneticAzimuth(float[] g, float[] m) {
        // getRotationMatrix uses H = magnetic x gravity, N = gravity x H.
        double hx = m[1]*g[2]-m[2]*g[1];
        double hy = m[2]*g[0]-m[0]*g[2];
        double hz = m[0]*g[1]-m[1]*g[0];
        double hn = Math.sqrt(hx*hx+hy*hy+hz*hz), gn = Math.sqrt(dot(g,g));
        double northY = (g[2]*hx-g[0]*hz)/(gn*hn);
        return Math.toDegrees(Math.atan2(hy/hn, northY));
    }
    public static void main(String[] args) {
        Heading h = new Heading();
        for (int angle = 0; angle < 360; angle++) {
            h.set(angle, true);
            float[] q = h.rotation(), g = h.gravity(), m = h.magnetic();
            close(dot(q,q)+q[3]*q[3], 1, 1e-6);
            close(Math.sqrt(dot(g,g)), 9.81, 1e-5);
            close(Math.sqrt(dot(m,m)), 50, 1e-5);
            double rotationAzimuth = Math.toDegrees(Math.atan2(2*(q[0]*q[1]-q[2]*q[3]),
                1-2*(q[0]*q[0]+q[2]*q[2])));
            close(difference(rotationAzimuth, angle), 0, 1e-4);
            close(difference(magneticAzimuth(g,m), angle), 0, 1e-4);
            close(h.orientation()[0], angle, 1e-5);
            float[] a = h.acceleration(1.5, 14, 2);
            close(dot(a,a), 1.5*1.5+14*14+2*2, 1e-4);
            if (angle % 30 == 0) {
                for (double length : new double[]{.5,1,2}) {
                    for (int phase=0;phase<320;phase++) {
                        float[] moving = h.gait(phase/40.,length);
                        close(difference(magneticAzimuth(moving,m),angle),0,3);
                    }
                }
            }
        }
        long now = 1;
        for (int[] pair : new int[][]{{359,1},{1,359},{270,0},{0,270}}) {
            h.set(pair[0], true); h.tick(now);
            h.set(pair[1], false);
            double last = h.azimuth(), direction = Math.signum(difference(pair[1], pair[0]));
            for (int i=0;i<150;i++) {
                now += 20000000L; h.tick(now);
                double change = difference(h.azimuth(), last);
                if (change*direction < -1e-4 || Math.abs(change) > 3.601)
                    throw new AssertionError("wrong arc or angular speed: " + change);
                last = h.azimuth();
            }
            close(difference(h.azimuth(),pair[1]),0,.001);
            float atRest = h.azimuth();
            now += 60000000000L; h.tick(now);
            close(difference(h.azimuth(),atRest),0,.001);
        }
        try { h.set(Double.NaN, true); throw new AssertionError("NaN accepted"); }
        catch (IllegalArgumentException expected) {}
    }
}
