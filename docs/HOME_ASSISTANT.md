# Home Assistant integration

## Entities PLATE publishes

| Entity | Unit | What it is |
|---|---|---|
| `sensor.plate_calorie_target` | kcal | Today's target, including any add-back for unusual activity. Carries a `plan_issues` attribute when the planner had to compromise. |
| `sensor.plate_calories_eaten` | kcal | Logged so far today |
| `sensor.plate_calories_remaining` | kcal | Target minus eaten |
| `sensor.plate_protein_eaten` / `_remaining` | g | |
| `sensor.plate_sodium_today` | mg | |
| `sensor.plate_potassium_today` | mg | |
| `sensor.plate_fiber_today` | g | |
| `sensor.plate_weight_trend` | lb | Smoothed trend, not a raw scale reading |
| `sensor.plate_weekly_rate` | lb | Observed rate of change per week |
| `sensor.plate_tdee` | kcal | Calibrated expenditure |
| `sensor.plate_tracker_bias` | % | How wrong your watch is. Positive = reads low. |
| `sensor.plate_calibration_confidence` | % | How much weight the calibration carries |
| `sensor.plate_adherence_7d` | % | Days in the last week within 10% of target |
| `sensor.plate_dash_score` | | Today's DASH pattern alignment, 0–100 |
| `sensor.plate_next_meal` | | Title, with meal/kcal/protein/servings attributes |
| `sensor.plate_bp_systolic_avg` / `_diastolic_avg` | mmHg | 14-day averages |
| `sensor.plate_bp_category` | | Normal / Elevated / Stage 1 / Stage 2 / Crisis |
| `sensor.plate_shopping_items` | | Unticked items on the current list |
| `binary_sensor.plate_shopping_needed` | | `on` when anything is outstanding |

## REST vs MQTT — pick deliberately

**REST** (the default) needs no configuration. But entities created through
`POST /api/states` aren't owned by an integration, so Home Assistant **forgets
them on restart** and they never enter long-term statistics. They're fine for a
card you glance at; useless for a history graph. PLATE re-publishes every 15
minutes, so the gap after a restart is minutes rather than permanent.

**MQTT discovery** needs a broker but produces real, durable entities with proper
device grouping, units and state classes — recorded, graphable, and reliable in
automations. If you already run Mosquitto:

```yaml
mqtt:
  enabled: true
  host: core-mosquitto
  port: 1883
  username: your_mqtt_user
  password: your_mqtt_password
```

Both run at once when configured. The MQTT copy claims the entity ids; the REST
copy keeps updating the same ids and keeps working if the broker goes down.

## A dashboard card

```yaml
type: vertical-stack
cards:
  - type: gauge
    entity: sensor.plate_calories_remaining
    name: Calories left
    min: 0
    max: 2600
    severity:
      green: 200
      yellow: 0
      red: -400

  - type: entities
    title: Today
    entities:
      - entity: sensor.plate_next_meal
        name: Up next
      - entity: sensor.plate_protein_remaining
        name: Protein to go
      - entity: sensor.plate_sodium_today
        name: Sodium
      - entity: sensor.plate_dash_score
        name: DASH score

  - type: entities
    title: Progress
    entities:
      - entity: sensor.plate_weight_trend
      - entity: sensor.plate_weekly_rate
      - entity: sensor.plate_tdee
        name: Calibrated TDEE
      - entity: sensor.plate_tracker_bias
        name: Fitbit error
      - entity: sensor.plate_adherence_7d

  - type: history-graph
    hours_to_show: 720
    entities:
      - sensor.plate_weight_trend
      - sensor.plate_tdee
```

The history graph only works with MQTT enabled — REST-created states don't get
recorded.

## Automations worth having

**Remind me to log dinner.**

```yaml
alias: PLATE — log dinner
triggers:
  - trigger: time
    at: "20:30:00"
conditions:
  - condition: numeric_state
    entity_id: sensor.plate_calories_remaining
    above: 400
actions:
  - action: notify.mobile_app_phone
    data:
      title: Still {{ states('sensor.plate_calories_remaining') }} kcal to go
      message: >-
        {{ state_attr('sensor.plate_next_meal', 'meal') | title }}:
        {{ states('sensor.plate_next_meal') }}
```

**Shopping reminder when you get near the store.** Pair
`binary_sensor.plate_shopping_needed` with a zone trigger:

```yaml
alias: PLATE — shopping nearby
triggers:
  - trigger: zone
    entity_id: person.you
    zone: zone.trader_joes
    event: enter
conditions:
  - condition: state
    entity_id: binary_sensor.plate_shopping_needed
    state: "on"
actions:
  - action: notify.mobile_app_phone
    data:
      message: >-
        {{ states('sensor.plate_shopping_items') }} items still on the list.
```

**Tell me when the calibration has learned something.** Tracker bias crossing
±10% means your targets just moved for a real reason:

```yaml
alias: PLATE — tracker bias found
triggers:
  - trigger: numeric_state
    entity_id: sensor.plate_tracker_bias
    above: 10
  - trigger: numeric_state
    entity_id: sensor.plate_tracker_bias
    below: -10
conditions:
  - condition: numeric_state
    entity_id: sensor.plate_calibration_confidence
    above: 60
actions:
  - action: notify.mobile_app_phone
    data:
      title: PLATE recalibrated
      message: >-
        Your tracker reads {{ states('sensor.plate_tracker_bias') | abs }}%
        {{ 'low' if states('sensor.plate_tracker_bias') | float > 0 else 'high' }}.
        Target is now {{ states('sensor.plate_calorie_target') }} kcal.
```

**Flag a plan that couldn't hit its constraints.**

```yaml
alias: PLATE — plan compromised
triggers:
  - trigger: state
    entity_id: sensor.plate_calorie_target
conditions:
  - condition: template
    value_template: >-
      {{ state_attr('sensor.plate_calorie_target', 'plan_issues') | length > 3 }}
actions:
  - action: persistent_notification.create
    data:
      title: PLATE had trouble planning this week
      message: >-
        {{ state_attr('sensor.plate_calorie_target', 'plan_issues') | join('\n') }}
```

## Getting enough history

The calibration wants ~28 days of weight and food logs. Home Assistant's
`recorder` keeps detailed history for 10 days by default, so PLATE reads
**long-term statistics** over the WebSocket API instead — those are retained
indefinitely for any sensor with a `state_class`, and PLATE mirrors everything
into its own database so it only needs to fetch each day once.

If your scale integration doesn't set a `state_class`, statistics aren't kept and
only the recorder window is recoverable. You can fix that with a template sensor:

```yaml
template:
  - sensor:
      - name: Weight for statistics
        unique_id: weight_for_statistics
        state: "{{ states('sensor.your_scale_weight') }}"
        unit_of_measurement: lb
        device_class: weight
        state_class: measurement
```

Point PLATE at that one instead. It starts accumulating from the day you create
it, so the sooner the better.

## Troubleshooting

**"No weight history yet."** The weight entity isn't set, or has never had a
numeric state. Settings → Home Assistant entities.

**Targets say `source: formula`.** No tracker calories configured, so PLATE is
using Mifflin-St Jeor scaled by estimated activity. Set the calories entity.

**Targets say `source: tracker` and never become `calibrated`.** Not enough food
logs — it needs roughly 60% of days in a four-week window. The Insight screen
shows how many days you have.

**Sensors vanished after restarting HA.** Expected with REST publishing; they come
back within 15 minutes. Enable MQTT for durable entities.

**Add-on won't start.** Check the log. The most common cause is a malformed YAML
file in your config folder — the app falls back to bundled data and reports the
error on the Insight screen rather than refusing to boot.
