import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import java.io.FileInputStream;
import java.io.InputStream;

/** Runs under app_process and uses Android's normal AudioFlinger speaker path. */
public final class IntercomAudio {
    public static void main(String[] args) throws Exception {
        int rate = Integer.parseInt(args[0]);
        InputStream input = args.length > 1 ? new FileInputStream(args[1]) : System.in;
        int minimum = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) throw new IllegalStateException("Invalid AudioTrack buffer: " + minimum);
        AudioTrack track = new AudioTrack(AudioManager.STREAM_MUSIC, rate,
                AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT,
                Math.max(minimum, rate / 5 * 4), AudioTrack.MODE_STREAM);
        if (track.getState() != AudioTrack.STATE_INITIALIZED)
            throw new IllegalStateException("AudioTrack initialization failed");
        long frames = 0;
        try {
            track.play();
            System.err.println("AUDIO_READY");
            byte[] buffer = new byte[1924];
            int carry = 0;
            while (true) {
                int count = input.read(buffer, carry, 1920 - carry);
                if (count < 0) break;
                count += carry;
                int complete = count / 4 * 4;
                int offset = 0;
                while (offset < complete) {
                    int written = track.write(buffer, offset, complete - offset);
                    if (written <= 0) throw new IllegalStateException("AudioTrack.write: " + written);
                    offset += written;
                }
                frames += complete / 4;
                carry = count - complete;
                if (carry > 0) System.arraycopy(buffer, complete, buffer, 0, carry);
            }
            long deadline = System.currentTimeMillis() + 3000;
            while ((track.getPlaybackHeadPosition() & 0xffffffffL) < frames
                    && System.currentTimeMillis() < deadline) Thread.sleep(10);
            System.err.println("AUDIO_DONE frames=" + frames + " played="
                    + (track.getPlaybackHeadPosition() & 0xffffffffL));
        } finally {
            track.stop();
            track.release();
            if (input != System.in) input.close();
        }
    }
}
