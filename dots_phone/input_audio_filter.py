"""Optional local denoising; off loads no native runtime or model."""

from dots_phone.config import Settings


def input_filter_factory(settings: Settings):
    if settings.pipecat_input_filter == "off":
        return None
    if settings.pipecat_input_filter != "deepfilter":
        raise ValueError("unknown input audio filter")
    from dots_phone.deepfilter_filter import deepfilter_factory

    return deepfilter_factory(settings)
