from __future__ import annotations

import sys

# Audio Stream & PCM Specifications
DEFAULT_UDP_PORT: int = 5005
DEFAULT_CONTROL_PORT: int = 5006
SAMPLE_RATE: int = 48000
CHANNELS: int = 1
BYTES_PER_SAMPLE: int = 2  # 16-bit PCM (s16le)
PCM_DTYPE: str = "<i2"

# Audio buffer & packet sizes
CHUNK_SAMPLES: int = 960  # 20ms at 48000Hz
DEFAULT_BUFFER_SIZE: int = 262144  # 256 KB socket rcvbuf

# Device naming
VIRTUAL_SINK_NAME: str = "virtual_mic_cel_sink"
VIRTUAL_SOURCE_NAME: str = "virtual_mic_cel_source"
VIRTUAL_MIC_DESCRIPTION: str = "Virtual_Mic_Cel"

# Platform detection
IS_LINUX: bool = sys.platform.startswith("linux")
IS_WINDOWS: bool = sys.platform.startswith("win")
CURRENT_PLATFORM: str = "linux" if IS_LINUX else ("windows" if IS_WINDOWS else sys.platform)
