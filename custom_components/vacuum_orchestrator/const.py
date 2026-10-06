"""Constants for Vacuum Orchestrator."""

DOMAIN = "vacuum_orchestrator"
INTEGRATION_VERSION = "0.1.0"
API_VERSION = 3
SIGNAL_VIEW_CHANGED = f"{DOMAIN}_view_changed"
CONFIG_ENTRY_VERSION = 2
CONFIG_ENTRY_MINOR_VERSION = 1
STORE_VERSION = 4
STORE_MINOR_VERSION = 0
CONF_INSTALLATION_ID = "installation_id"
CONF_ROBOT_ENTITY_ID = "robot_entity_id"
CONF_ROBOT_REGISTRY_ID = "robot_registry_id"
CONF_ADAPTER = "adapter"
CONF_TARGET_AREAS = "target_areas"
CONF_LAST_CLEAN_START_ENTITY_ID = "last_clean_start_entity_id"
CONF_LAST_CLEAN_END_ENTITY_ID = "last_clean_end_entity_id"

SUBENTRY_TYPE_ROBOT = "robot"
ADAPTER_ROBOROCK = "roborock"

SERVICE_CREATE_JOB = "create_job"
SERVICE_UPDATE_JOB = "update_job"
SERVICE_DELETE_JOB = "delete_job"
SERVICE_MOVE_JOB = "move_job"
SERVICE_START_JOB = "start_job"
SERVICE_RUN_QUEUE = "run_queue"
SERVICE_PAUSE_QUEUE = "pause_queue"
SERVICE_RESUME_QUEUE = "resume_queue"
SERVICE_CANCEL_JOB = "cancel_job"
SERVICE_RETRY_JOB = "retry_job"
SERVICE_GET_QUEUE = "get_queue"
SERVICE_GET_JOB = "get_job"

PLATFORMS = ("sensor", "binary_sensor", "switch")
