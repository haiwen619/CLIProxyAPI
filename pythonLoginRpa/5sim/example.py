"""
5sim.net API 使用示例

运行前请先设置 API Token:
  1. 登录 https://5sim.net
  2. 点击右上角头像 → "Get API key"
  3. 复制 Token，替换下方 API_KEY
"""
from client import FiveSimClient, FiveSimError

API_KEY = "eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE4MDY3MjQ2OTIsImlhdCI6MTc3NTE4ODY5MiwicmF5IjoiNTRhYTAzMjdhOGY3NmMxMWNjNmI0MTUzYzE4YWU5NmUiLCJzdWIiOjM3Nzk2NTZ9.C8UoI8Mp0nwpqC6Wpg98Cv98IK9rcGTGNskzJi00JOveOr0XFA8Mfwcvqk22gvGhuD2jEQeoGAgKBR7asa9Cm9G8j4hvEVL8dchRIb8s9FEux06pL91OVzOmKGrFoaYapSaBTfSbp3EjzKkZ6E9S1KnTnvbWfWsoEz-xjlvszeFvwClvjUfXo5IFpX4gDIp0AIbnSEZTKOgBU-z5o0IgV1gJUNOLD50CaFoZw-fZUw5bz_WUTPM8pyW55H9QBt2nt9B9FcAGgeo52PfwN3u5wcjP8p5iX45ZwJ7jtu56kW9DzSbgCRSvYAD0pA4jWTeUzShzjaOd9cxSFBrzaKoE9w"


def example_get_balance():
    """示例：查询账户余额"""
    client = FiveSimClient(API_KEY)
    profile = client.get_profile()
    print("邮箱:", profile["email"])
    print("余额:", profile["balance"])
    print("评分:", profile["rating"])


def example_buy_and_receive_sms():
    """示例：购买号码并等待接收短信（OpenAI/ChatGPT，美国，Virtual63）"""
    client = FiveSimClient(API_KEY)

    # 1. 查看荷兰 virtual59 可用产品（可选）
    products = client.get_products(country="usa", operator="virtual63")
    openai_info = products.get("openai")
    if openai_info:
        print(f"usa/virtual63/openai: 价格={openai_info.get('Price')}  库存={openai_info.get('Qty')}")

    # 2. 购买激活号码
    print("\n正在购买号码...")
    order = client.buy_activation(
        country="usa",
        operator="virtual63",
        product="openai",
    )
    order_id = order["id"]
    phone = order["phone"]
    print(f"已分配号码: {phone}  (订单 ID: {order_id})")

    # 3. 使用该号码在 OpenAI 注册，然后轮询等待短信
    print("等待短信到达（最多 5 分钟）...")
    try:
        order = client.wait_for_sms(order_id, timeout=300, interval=10)
        sms_list = order["sms"]
        print(f"收到 {len(sms_list)} 条短信:")
        for sms in sms_list:
            print(f"  来自 {sms.get('sender')}: {sms.get('text')}")

        # 4. 订单将在 20 分钟后自动到期，无需手动完成

    except TimeoutError as e:
        print(f"超时: {e}")
        # 超时则取消订单以退款
        client.cancel_order(order_id)
        print("订单已取消（已退款）")

    except FiveSimError as e:
        msg = str(e)
        print(f"API 错误: {e}")
        if "BANNED" in msg:
            # 号码已被拉黑，举报并退款
            try:
                client.ban_order(order_id)
                print(f"号码已举报为封禁（订单 {order_id} 已退款）")
            except FiveSimError as be:
                print(f"举报失败: {be}")
        elif "CANCELED" in msg:
            print(f"订单 {order_id} 已被取消，无需再次操作")


def example_cancel_order(order_id: int):
    """示例：主动取消指定订单（号码失效/无法使用时退款）"""
    client = FiveSimClient(API_KEY)

    # 先查询当前状态
    try:
        order = client.check_order(order_id)
        status = order.get("status", "UNKNOWN")
        phone = order.get("phone", "")
        print(f"订单 {order_id}  号码: {phone}  当前状态: {status}")
    except FiveSimError as e:
        print(f"查询订单失败: {e}")
        return

    if status in ("FINISHED", "CANCELED", "TIMEOUT"):
        print(f"订单已处于终态（{status}），无法取消")
        return

    if status == "BANNED":
        # 号码被拉黑 → 使用 ban_order 举报并退款
        try:
            result = client.ban_order(order_id)
            print(f"已举报封禁号码，退款状态: {result.get('status')}")
        except FiveSimError as e:
            print(f"举报失败: {e}")
    else:
        # PENDING / RECEIVED 等正常状态 → 普通取消退款
        try:
            result = client.cancel_order(order_id)
            print(f"订单已取消，退款状态: {result.get('status')}")
        except FiveSimError as e:
            print(f"取消失败: {e}")


def example_check_prices():
    """示例：查询印度 Telegram 价格，并列出可用产品"""
    client = FiveSimClient(API_KEY)

    # 1. get_prices 按国家+产品过滤
    prices = client.get_prices(product="telegram", country="india")
    # 结构: {country: {product: {operator: {cost, count, rate}}}}
    operators = prices.get("india", {}).get("telegram", {})
    print("india/telegram 各运营商价格:")
    for op, info in operators.items():
        print(f"  {op}: cost={info.get('cost')}  库存={info.get('count')}")

    # 2. get_products 查看可购买产品列表（路径参数）
    products = client.get_products(country="india", operator="any")
    print("\nindia/any 可用产品（前 5 个）:")
    for name, info in list(products.items())[:5]:
        # 字段名为大写：Price, Qty, Category
        print(f"  {name}: 价格={info.get('Price')}  库存={info.get('Qty')}")


def example_orders_history():
    """示例：查看最近 5 条订单历史"""
    client = FiveSimClient(API_KEY)
    result = client.get_orders_history(category="activation", limit=5)
    orders = result.get("Data", [])
    print(f"共 {result.get('Total', 0)} 条历史订单，显示最近 {len(orders)} 条:")
    for o in orders:
        print(f"  [{o['id']}] {o['phone']}  {o['product']}  {o['status']}  {o['created_at']}")


if __name__ == "__main__":
    print("=" * 50)
    print("示例 1: 查询余额")
    print("=" * 50)
    example_get_balance()

    # print("\n" + "=" * 50)
    # print("示例 2: 查询 Telegram 价格（无需购买）")
    # print("=" * 50)
    # example_check_prices()

    # print("\n" + "=" * 50)
    # print("示例 3: 查看订单历史")
    # print("=" * 50)
    # example_orders_history()

    # 取消注释以测试购买流程（会消耗余额）
    print("\n" + "=" * 50)
    print("示例 4: 购买号码并接收短信")
    print("=" * 50)
    example_buy_and_receive_sms()

    # 取消注释并填入订单 ID，可主动取消失效/被拉黑的订单
    # print("\n" + "=" * 50)
    # print("示例 5: 取消指定订单（号码失效/被拉黑时退款）")
    # print("=" * 50)
    # example_cancel_order(984206469)  # ← 替换为你的订单 ID
