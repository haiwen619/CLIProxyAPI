"""
5sim.net API Client
官方文档: https://5sim.net/docs
"""
import time
import requests


BASE_URL = "https://5sim.net/v1"


class FiveSimError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


class FiveSimClient:
    """
    5sim.net API 客户端

    用法:
        client = FiveSimClient(api_key="your_token_here")
        profile = client.get_profile()
        print(profile["balance"])
    """

    def __init__(self, api_key: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if not resp.ok:
            raise FiveSimError(resp.status_code, resp.text)
        return resp.json()

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    # -------------------------------------------------------------------------
    # 用户信息
    # -------------------------------------------------------------------------

    def get_profile(self) -> dict:
        """
        获取用户资料（余额、邮箱、评分等）
        返回字段: id, email, vendor, balance, rating, default_country, default_operator
        """
        return self._get("/user/profile")

    def get_balance(self) -> float:
        """获取账户余额（快捷方法）"""
        return self.get_profile()["balance"]

    def get_orders_history(
        self,
        category: str = "activation",
        limit: int = 15,
        offset: int = 0,
        order: str = "id",
        reverse: bool = False,
    ) -> dict:
        """
        获取订单历史
        category: activation | hosting
        """
        return self._get("/user/orders", params={
            "category": category,
            "limit": limit,
            "offset": offset,
            "order": order,
            "reverse": str(reverse).lower(),
        })

    def get_payment_history(self) -> dict:
        """获取充值/支付历史"""
        return self._get("/user/payments")

    def get_sms_inbox(self, order_id: int) -> dict:
        """
        获取短信收件箱（仅限租用号码，不适用于激活号码）
        """
        return self._get(f"/user/sms/inbox/{order_id}")

    # -------------------------------------------------------------------------
    # 购买号码
    # -------------------------------------------------------------------------

    def buy_activation(self, country: str, operator: str, product: str) -> dict:
        """
        购买一次性激活号码
        country:  国家名称，如 russia / china / any
        operator: 运营商名称，如 any / mts / beeline
        product:  服务名称，如 telegram / whatsapp / vk

        返回字段: id, phone, operator, product, price, status, expires, sms, created_at
        """
        return self._get(f"/user/buy/activation/{country}/{operator}/{product}")

    def buy_hosting(self, country: str, operator: str, product: str) -> dict:
        """
        购买租用号码（可多次接收短信）
        参数同 buy_activation
        """
        return self._get(f"/user/buy/hosting/{country}/{operator}/{product}")

    # -------------------------------------------------------------------------
    # 订单管理
    # -------------------------------------------------------------------------

    def check_order(self, order_id: int) -> dict:
        """
        检查订单状态 / 获取已接收的短信
        status: PENDING / RECEIVED / CANCELED / TIMEOUT / FINISHED / BANNED
        """
        return self._get(f"/user/check/{order_id}")

    def finish_order(self, order_id: int) -> dict:
        """收到验证码后，标记订单完成"""
        return self._get(f"/user/finish/{order_id}")

    def cancel_order(self, order_id: int) -> dict:
        """取消订单（未收到短信时退款）"""
        return self._get(f"/user/cancel/{order_id}")

    def ban_order(self, order_id: int) -> dict:
        """举报号码已被封禁（退款并标记该号码）"""
        return self._get(f"/user/ban/{order_id}")

    # -------------------------------------------------------------------------
    # 等待短信（轮询）
    # -------------------------------------------------------------------------

    def wait_for_sms(
        self,
        order_id: int,
        timeout: int = 300,
        interval: int = 10,
    ) -> dict:
        """
        轮询等待短信到达，返回包含短信的订单信息。
        timeout:  最大等待秒数（默认 300 秒）
        interval: 轮询间隔秒数（默认 10 秒）
        抛出 TimeoutError 如果超时未收到短信。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            order = self.check_order(order_id)
            status = order.get("status", "")
            if status in ("CANCELED", "TIMEOUT", "BANNED"):
                raise FiveSimError(0, f"订单已终止，状态: {status}")
            if order.get("sms"):
                return order
            time.sleep(interval)
        raise TimeoutError(f"等待短信超时（{timeout}s），订单 ID: {order_id}")

    # -------------------------------------------------------------------------
    # 公开接口（无需认证）
    # -------------------------------------------------------------------------

    def get_countries(self) -> dict:
        """获取所有可用国家列表"""
        return self._get("/guest/countries")

    def get_products(self, country: str, operator: str = "any") -> dict:
        """
        获取指定国家/运营商的可购买产品列表
        country:  如 russia / china / any
        operator: 如 any / mts
        """
        return self._get(f"/guest/products/{country}/{operator}")

    def get_prices(
        self,
        product: str = None,
        operator: str = None,
        country: str = None,
    ) -> dict:
        """
        获取产品价格
        可选参数过滤: product, operator, country
        """
        params = {}
        if product:
            params["product"] = product
        if operator:
            params["operator"] = operator
        if country:
            params["country"] = country
        return self._get("/guest/prices", params=params or None)

    def get_flash(self, lang: str = "zh") -> dict:
        """获取系统通知/公告（lang: zh / en / ru 等）"""
        return self._get(f"/guest/flash/{lang}")

    # -------------------------------------------------------------------------
    # 供应商接口
    # -------------------------------------------------------------------------

    def get_vendor_wallets(self) -> dict:
        """获取供应商钱包余额（需要供应商账户）"""
        return self._get("/vendor/wallets")

    def get_vendor_orders(self, category: str = "activation") -> dict:
        """获取供应商订单列表"""
        return self._get("/vendor/orders", params={"category": category})
