package users;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class DigestJob {

    @Scheduled(cron = "0 0 8 * * *")
    public void sendDailyDigest() {
        // Spring @Scheduled cron trigger — 08:00 daily.
        buildDigest();
    }

    @KafkaListener(topics = "user-events")
    public void onUserEvent(String payload) {
        buildDigest();
    }

    public void buildDigest() {
    }
}
