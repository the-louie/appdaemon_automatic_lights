# Automatic Lights - Home Assistant AppDaemon App

Copyright (c) 2025 the_louie

## Overview

AutomaticLights is a Home Assistant AppDaemon application that provides intelligent lighting control based on time, sun position, and optional solar radiation sensors. The app manages four distinct lighting modes throughout the day:

- **Night**: Low ambient lighting for late night hours
- **Morning**: Gentle wake-up lighting before sunrise
- **Day**: Full lighting during daylight hours
- **Evening**: Transitional lighting as daylight fades

## Features

- **Time-based triggers**: Automatic morning and night scene activation
- **Sun position monitoring**: Uses solar elevation and rising status for precise timing
- **Solar radiation integration**: Optional light level sensor integration for enhanced accuracy
- **Manual scene override**: Support for manual scene activation
- **Robust error handling**: Comprehensive logging and error recovery
- **Configurable thresholds**: Customizable elevation and light level thresholds

## Configuration

### Basic Configuration

```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "05:30"
  night_start: "23:30"
  scenes:
    night:
      group.living_room: false
      group.bedroom: false
      group.kitchen: true
    morning:
      group.living_room: true
      group.bedroom: true
      group.kitchen: true
    day:
      group.living_room: false
      group.bedroom: false
      group.kitchen: false
    evening:
      group.living_room: true
      group.bedroom: false
      group.kitchen: true
```

### Advanced Configuration with Solar Radiation

```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "05:30"
  night_start: "23:30"
  solar_radiation:
    sensor: "sensor.outdoor_light_level"
    threshold: 500
    elevation_threshold: 3
  scenes:
    # ... scene configurations
```

## Configuration Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `morning_start` | string | Yes | "05:30" | Time to start morning scene (HH:MM format) |
| `night_start` | string | Yes | "23:30" | Time to start night scene (HH:MM format) |
| `solar_radiation` | dict | No | None | Solar radiation configuration |
| `scenes` | dict | Yes | {} | Scene configurations mapping groups to states |

### Solar Radiation Configuration

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sensor` | string | Yes | - | Entity ID of the light level sensor |
| `threshold` | float | Yes | - | Light level threshold for transitions |
| `elevation_threshold` | float | No | 3 | Solar elevation threshold in degrees |

## State Transitions

### Time-based Transitions
- **Night → Morning**: Triggered at `morning_start` time
- **Evening → Night**: Triggered at `night_start` time

### Sensor-based Transitions
- **Morning → Day**: When sun is rising AND elevation > threshold AND (light level > threshold OR solar radiation disabled)
- **Day → Evening**: When sun is not rising AND (light level < threshold OR elevation < threshold)

## Dependencies

- Home Assistant
- AppDaemon 4.x
- Python 3.7+

## Installation

1. Copy `i1_automatic_lights.py` to your AppDaemon `apps` directory
2. Add configuration to your `apps.yaml` file
3. Restart AppDaemon

## Logging

The app provides extensive enterprise-grade logging including:
- Configuration validation
- State transition events
- Sensor value monitoring
- Error conditions with line numbers
- Performance metrics

## Error Handling

The app includes comprehensive error handling for:
- Invalid configuration parameters
- Missing or unavailable sensors
- Network connectivity issues
- Invalid scene configurations
- Type validation for all inputs

## License

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please ensure all code follows PEP8 style guidelines and includes appropriate error handling and logging.