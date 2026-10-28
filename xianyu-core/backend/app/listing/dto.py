"""闲鱼商品发布请求体 DTO（照抄 protocol.md §3.1，蓝本 goofish-cli publish.py）。

字段序敏感（参与签名 = 紧凑 JSON 原样序列化），勿调整字段定义顺序。
"""

from pydantic import BaseModel, Field


class ImageExtraInfo(BaseModel):
    isH: str = "false"
    isT: str = "false"
    raw: str = "false"


class ImageInfoDO(BaseModel):
    url: str
    major: bool = True             # 是否主图（首图）
    type: int = 0
    status: str = "done"
    isQrCode: bool = False
    heightSize: int = 0
    widthSize: int = 0
    extraInfo: ImageExtraInfo = Field(default_factory=ImageExtraInfo)


class ItemTextDTO(BaseModel):
    title: str
    desc: str
    titleDescSeparate: bool = True


class ItemPriceDTO(BaseModel):
    # 价格单位"分"，字符串传输（元×100）
    priceInCent: str
    origPriceInCent: str | None = None


class ItemPostFeeDTO(BaseModel):
    """运费四态（protocol.md §3.1）：
    - 包邮（默认）：canFreeShipping + supportFreight；
    - 按距离：supportFreight + templateId "-100"；
    - 一口价：postPriceInCent + templateId "0"；
    - 无需邮寄（虚拟商品）：templateId "0" + 虚拟类目。
    """

    canFreeShipping: bool = True
    supportFreight: bool = True
    postPriceInCent: str | None = None
    templateId: str | None = None

    @classmethod
    def free(cls) -> "ItemPostFeeDTO":
        return cls()

    @classmethod
    def no_shipping(cls) -> "ItemPostFeeDTO":
        """虚拟商品：无需邮寄。"""
        return cls(canFreeShipping=False, supportFreight=False, templateId="0")

    @classmethod
    def fixed(cls, post_price_cent: int) -> "ItemPostFeeDTO":
        """一口价运费。"""
        return cls(
            canFreeShipping=False,
            supportFreight=True,
            postPriceInCent=str(post_price_cent),
            templateId="0",
        )


class ItemCatDTO(BaseModel):
    catId: str
    catName: str
    channelCatId: str | None = None
    tbCatId: str | None = None


class ItemAddrDTO(BaseModel):
    prov: str
    city: str
    area: str
    divisionId: str
    gps: str | None = None
    poiId: str | None = None
    poiName: str | None = None


class PublishItemRequest(BaseModel):
    """mtop.idle.pc.idleitem.publish v1.0 请求体（protocol.md §3.1 [待验证]）。"""

    imageInfoDOList: list[ImageInfoDO]
    itemTextDTO: ItemTextDTO
    itemPriceDTO: ItemPriceDTO
    itemPostFeeDTO: ItemPostFeeDTO
    itemCatDTO: ItemCatDTO
    itemAddrDTO: ItemAddrDTO
    itemSkuList: list = Field(default_factory=list)
    quantity: str = "1"
    itemTypeStr: str = "b"
    simpleItem: str = "true"
    onlyTakeSelf: bool = True
    freebies: bool = False
    userRightsProtocols: list = Field(default_factory=list)
    sourceId: str = ""
    bizcode: str = ""
    publishScene: str = "pcMainPublish"
    uniqueCode: str = Field(default="1775897582791680", description="参考项目硬编码值 [待验证]")
