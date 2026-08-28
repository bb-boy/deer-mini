"""
输入
- profile_name：Run.model_name，例如 ustc-deepseek-flash。
输出
一个符合 ChatModel 协议的模型对象。
副作用
- 读取 .env；
- 将环境变量加载进当前 Python 进程；
- 创建模型客户端；

"""

from dotenv import load_dotenv
import os

from app.model.base import ChatModel
from app.model.openai_compatible import OpenAICompatibleModel
from app.model.config import DEFAULT_MODEL_NAME, get_model_profile

class ModelFactory:

    def __init__(self,model_name: str = DEFAULT_MODEL_NAME):

        
        self._model_name = model_name
       

    
    def create_chat_model(self) -> ChatModel:
        """
        根据 modelname 创建模型对象。
        """
        # 读取 .env 文件，并将环境变量加载进当前 Python 进程
        load_dotenv()


        profile = get_model_profile(self._model_name)
        
            
        
        # 获取模型配置文件
        

        api_key = os.getenv(profile.api_key_env)
        base_url = os.getenv(profile.base_url_env)
        
        if not api_key or not base_url:
            raise ValueError(f"环境变量 {profile.api_key_env} 和 {profile.base_url_env} 必须设置")


        if profile.provider == "openai_compatible":
          
            # 创建 OpenAICompatibleModel 实例
            return OpenAICompatibleModel(
                api_key=api_key,
                base_url=base_url, 
                profile=profile
            )


        raise ValueError(
        f"暂不支持模型提供商：{profile.provider}"
    )