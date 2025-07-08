# Automatic Lights - Home Assistant AppDaemon App

## Summary

Automatic Lights is a sophisticated Home Assistant AppDaemon application that provides intelligent, time-based and sensor-driven lighting control for your smart home. The app automatically manages lighting scenes throughout the day based on sunrise/sunset times, solar radiation levels, and user-defined schedules. It features four distinct lighting modes (night, morning, day, evening) that transition seamlessly based on environmental conditions and time of day.

The system uses a combination of time-based triggers and real-time sensor data to make intelligent decisions about when to activate different lighting scenes. It monitors solar elevation, sun position, and light levels to determine optimal lighting conditions, ensuring your home is always appropriately lit while maximizing energy efficiency. The app includes robust error handling, configurable thresholds, and support for manual scene activation, making it suitable for both automated and user-controlled lighting scenarios.

## Features

### 🌅 **Intelligent Time-Based Control**
- **Four Lighting Modes**: Night, Morning, Day, and Evening scenes
- **Automatic Transitions**: Seamless switching between lighting modes based on time and conditions
- **Configurable Schedules**: Customizable morning and night start times
- **Randomized Delays**: Prevents all lights from changing simultaneously for a more natural feel

### ☀️ **Solar-Powered Intelligence**
- **Solar Elevation Monitoring**: Tracks sun position for precise timing
- **Light Level Sensing**: Uses solar radiation sensors to determine ambient light conditions
- **Rising/Setting Detection**: Monitors sun movement for optimal transition timing
- **Environmental Adaptation**: Adjusts lighting based on actual light conditions, not just time

### 🎛️ **Manual Control Integration**
- **Scene Override**: Manual scene activation through Home Assistant
- **Immediate Response**: Instant lighting changes when manually triggered
- **State Synchronization**: Maintains consistency between automatic and manual control

### 🛡️ **Robust Error Handling**
- **Sensor Validation**: Gracefully handles unavailable or missing sensors
- **Cache Management**: 6-hour group cache with automatic refresh
- **Fallback Mechanisms**: Continues operation even with sensor failures
- **Comprehensive Logging**: Detailed logs for troubleshooting and monitoring

### ⚡ **Performance Optimizations**
- **Efficient Caching**: Reduces API calls to Home Assistant
- **Smart Group Management**: Caches entity groups for faster scene activation
- **Optimized State Checks**: Minimizes unnecessary sensor queries

## Installation

### Prerequisites
- Home Assistant with AppDaemon 4.x installed
- Python 3.8 or higher
- Required Home Assistant entities and sensors

### Setup Steps

1. **Install AppDaemon**
   ```bash
   pip install appdaemon
   ```

2. **Copy the Script**
   - Place `i1_automatic_lights.py` in your AppDaemon `apps` directory
   - Typically located at: `/config/appdaemon/apps/`

3. **Configure Home Assistant**
   - Ensure you have the required sensors configured
   - Set up lighting groups for scene control

4. **Add Configuration**
   - Add the app configuration to your `apps.yaml` file

## Configuration

### Basic Configuration

Add the following to your `apps.yaml`:

#### With Solar Radiation (Recommended)
```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "05:30"
  night_start: "23:30"
  solar_radiation:
    sensor: "sensor.solar_radiation"
    threshold: 100
  scenes:
    morning:
      living_room: true
      kitchen: true
      bedroom: false
    day:
      living_room: false
      kitchen: false
      bedroom: false
    evening:
      living_room: true
      kitchen: true
      bedroom: true
    night:
      living_room: false
      kitchen: false
      bedroom: true
```

#### Without Solar Radiation (Time and Sun Position Only)
```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "05:30"
  night_start: "23:30"
  scenes:
    morning:
      living_room: true
      kitchen: true
      bedroom: false
    day:
      living_room: false
      kitchen: false
      bedroom: false
    evening:
      living_room: true
      kitchen: true
      bedroom: true
    night:
      living_room: false
      kitchen: false
      bedroom: true
```

### Configuration Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `morning_start` | string | Yes | "05:30" | Time to start morning scene (HH:MM format) |
| `night_start` | string | Yes | "23:30" | Time to start night scene (HH:MM format) |
| `solar_radiation` | dict | No | - | Solar radiation sensor configuration (optional) |
| `solar_radiation.sensor` | string | Yes* | - | Entity ID of solar radiation sensor (*required if solar_radiation is configured) |
| `solar_radiation.threshold` | int | Yes* | - | Light level threshold for transitions (*required if solar_radiation is configured) |
| `scenes` | dict | Yes | - | Scene configurations for each lighting mode |

### Scene Configuration

Each scene (morning, day, evening, night) should contain a mapping of group names to boolean states:

```yaml
scenes:
  morning:
    group_name: true/false  # true = turn on, false = turn off
```

## Required Home Assistant Entities

### Sensors
- **`sensor.sun_solar_elevation`**: Solar elevation sensor
- **`sun.sun`**: Sun entity with `rising` attribute
- **Solar radiation sensor**: As specified in configuration (optional)

### Groups
- Lighting groups that correspond to your scene configuration
- Groups should contain the entities you want to control

### State Tracking
- **`irisone.time_state`**: Custom entity for tracking current lighting state

## How It Works

### State Calculation
The app calculates the current time-based state using this logic:

1. **Night**: Before sunrise and before morning start time
2. **Morning**: After morning start but before sunrise
3. **Day**: After sunrise but before sunset
4. **Evening**: After sunset but before night start time
5. **Night**: After night start time

### Transition Triggers

#### Time-Based Transitions
- **Morning Start**: Triggered at configured morning time with randomized delay
- **Night Start**: Triggered at configured night time with randomized delay

#### Sensor-Based Transitions
- **Morning to Day**: When sun is rising, elevation > 3°, and (light level > threshold if solar radiation enabled)
- **Day to Evening**: When sun is not rising (setting), and (light level < threshold if solar radiation enabled)
- **Without Solar Radiation**: Transitions based on sun elevation and rising status only

### Scene Activation Process
1. **State Update**: Updates current state and Home Assistant entity
2. **Group Cache Check**: Refreshes group cache if expired (6 hours)
3. **Entity Control**: Iterates through scene groups and controls individual entities
4. **Execution**: Either immediate or delayed execution based on `run_now` parameter

## Usage Examples

### Manual Scene Activation
```yaml
# In Home Assistant automation or script
service: scene.turn_on
target:
  entity_id: scene.morning
```

### Custom Transition Times
```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "06:00"  # Later morning start
  night_start: "22:00"    # Earlier night start
  # ... rest of configuration
```

### Different Light Thresholds
```yaml
solar_radiation:
  sensor: "sensor.outdoor_lux"
  threshold: 500  # Higher threshold for brighter conditions
```

### Minimal Configuration (No Solar Radiation)
```yaml
automatic_lights:
  module: i1_automatic_lights
  class: AutomaticLights
  morning_start: "06:00"
  night_start: "22:00"
  scenes:
    morning:
      living_room: true
    day:
      living_room: false
    evening:
      living_room: true
    night:
      living_room: false
```

## Troubleshooting

### Common Issues

#### Sensors Unavailable
```
Solar elevation sensor unavailable: None
```
**Solution**: Ensure the solar elevation sensor is properly configured and online.

#### Groups Not Found
```
ERROR: No entities for group living_room and scene morning
```
**Solution**: Verify that the group exists in Home Assistant and matches your configuration.

#### Scene Not Activating
- Check that the scene name in configuration matches the actual scene
- Verify that all required sensors are available
- Review logs for specific error messages

### Debugging

Enable detailed logging by checking the AppDaemon logs:
```bash
# View AppDaemon logs
tail -f /config/appdaemon/logs/appdaemon.log
```

### Performance Optimization

- **Cache Duration**: Adjust group cache timeout if needed
- **Sensor Polling**: Monitor sensor update frequency
- **Group Size**: Keep lighting groups reasonably sized

## Advanced Configuration

### Custom Transition Logic
The app can be extended to include additional transition conditions by modifying the `sun_pos` method.

### Multiple Instances
You can run multiple instances of the app for different areas of your home:

```yaml
automatic_lights_living:
  module: i1_automatic_lights
  class: AutomaticLights
  # Living room configuration

automatic_lights_bedroom:
  module: i1_automatic_lights
  class: AutomaticLights
  # Bedroom configuration
```

## Contributing

Contributions are welcome! Please feel free to submit issues, feature requests, or pull requests.

## License

This project is licensed under the BSD 2-Clause License - see the [LICENSE](LICENSE) file for details.

**Copyright (c) 2024, the_louie**

## Support

For support and questions:
- Check the troubleshooting section above
- Review the AppDaemon documentation
- Open an issue on the project repository

---

**Note**: This app requires a properly configured Home Assistant installation with AppDaemon. Make sure all required sensors and entities are available before deployment.