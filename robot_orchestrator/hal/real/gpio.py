import time


class RealGpioReader:
    def read(
        self,
        chip: str,
        line: int,
        active_low: bool,
        bias: str,
        samples: int,
        interval_ms: int,
    ) -> bool:
        import gpiod
        from gpiod.line import Bias, Direction, Value

        bias_map = {
            "pull-up": Bias.PULL_UP,
            "pull-down": Bias.PULL_DOWN,
            "disabled": Bias.DISABLED,
        }

        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            bias=bias_map[bias],
            active_low=active_low,
        )
        request = gpiod.request_lines(chip, consumer="robot-orchestrator", config={line: settings})
        try:
            active_count = 0
            for sample_index in range(samples):
                if request.get_value(line) == Value.ACTIVE:
                    active_count += 1
                if sample_index < samples - 1:
                    time.sleep(interval_ms / 1000.0)
        finally:
            request.release()

        return active_count * 2 > samples
