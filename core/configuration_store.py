import json
import os
import logging
from datetime import datetime
from core.urgency_config import UrgencyRulesConfig

logger = logging.getLogger(__name__)

class ConfigurationStore:
    def __init__(self, config_dir='config'):
        self.config_dir = config_dir
        self.config_path = os.path.join(config_dir, 'urgency_rules.json')
        self.changelog_path = os.path.join(config_dir, 'urgency_rules_changelog.json')
        self.temp_path = os.path.join(config_dir, 'urgency_rules.json.tmp')
    
    def load(self):
        if not os.path.exists(self.config_path):
            logger.info(f'Config file not found, using defaults')
            return (UrgencyRulesConfig.get_default(), False)
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            config = UrgencyRulesConfig.from_dict(data)
            is_valid, errors = config.validate()
            if not is_valid:
                logger.error(f'Loaded config is invalid: {errors}')
                return (UrgencyRulesConfig.get_default(), True)
            return (config, False)
        except Exception as e:
            logger.error(f'Failed to parse config: {e}')
            return (UrgencyRulesConfig.get_default(), True)
    
    def save(self, config, old_config=None):
        is_valid, errors = config.validate()
        if not is_valid:
            error_msg = '; '.join(errors)
            logger.error(f'Cannot save invalid config: {error_msg}')
            return (False, error_msg)
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            data = config.to_dict()
            with open(self.temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(self.temp_path, self.config_path)
            logger.info('Configuration saved successfully')
            
            # Record change in changelog
            if old_config is not None:
                changelog_success = self.append_to_changelog(old_config.to_dict(), data)
                if not changelog_success:
                    logger.warning('Configuration saved but changelog write failed')
            
            return (True, '')
        except Exception as e:
            logger.error(f'Failed to save: {e}')
            try:
                if os.path.exists(self.temp_path):
                    os.remove(self.temp_path)
            except:
                pass
            return (False, str(e))
    
    def append_to_changelog(self, old_config, new_config):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            entry = {
                'timestamp': datetime.now().isoformat(),
                'previous_settings': old_config,
                'new_settings': new_config
            }
            changelog = []
            if os.path.exists(self.changelog_path):
                try:
                    with open(self.changelog_path, 'r', encoding='utf-8') as f:
                        changelog = json.load(f)
                except:
                    pass
            changelog.append(entry)
            with open(self.changelog_path, 'w', encoding='utf-8') as f:
                json.dump(changelog, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.warning(f'Failed to write changelog: {e}')
            return False
