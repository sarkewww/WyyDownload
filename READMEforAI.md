# AI Context

如果你(AI)已经读取过本文件，那么接下来的开发中你只需要注意遵守本文件的规则，无需额外读取

## Project

WyyDownload

https://github.com/sarkewww/WyyDownload

https://github.com/sarkewww

## Goal

下载网易云音乐资源

下载其他平台音乐资源(future)

详见README.md

## Language

Python 3.10

## RULE

1. 上传Github仓库(git push)前必须经过我的许可
2. 添加/修改/删除任何功能或组件前必须经过我的许可,除非我让你这样做的
3. 注释应避免三引号注释,采用井号注释

## NOTICE

1. 版本迭代规则: 当项目底层基本重构时版本+1.0.0,当添加新功能时版本+0.1.0,当修复Bug数大于等于2时版本+0.0.1,每次版本碟后后应当在本地Commit
2. 本文件中的TODO并不是必须事项,而是开发者的TODO,可以在寻找新需求时提醒我TODO
3. 项目基于 https://github.com/Suxiaoqinx 的 https://github.com/Suxiaoqinx/Netease_url 再次开发,必要时要征求我的意见进行标注


## TODO

1. 下载任务管理,新增下载管理按钮,快捷管理下载任务(P0)
2. QQ音乐下载支持(P2)
3. 其他平台音乐下载支持,QQ,汽水,Apple,BiliBili Video, Youtube Music(P2.1)
4. 增加更多的设置,如代理服务器,下载位置等(P1)
5. 支持更多的登录方式,如微信登录,QQ登录,微博登录,网易邮箱账号登录,手机号登录(P3)
6. 保护本地cookie,随便加密一下cookie.txt,防止恶意软件读取,不过非要读取也没办法(P4)
7. 深色模式存在bug,在单曲中会有部分元素依旧保持白色(P99)


### !!!

### 8. 我真的受不了了,当前版本被我改的太屎山了,导致agent与我都没招维护了,周五我手动把代码改的模块化一点,都是vibecode害的我!!!

目前思路:将网易云下载有关的代码全重构为wyy.py,网页部分重构为wyy_web.py,main.py大多数职责作为入口代码使用,本地化数据采用sqlite,网页中音频下载等操作通过*web.py调用wyy.py函数,然后让agent帮我优化一下注释和代码就完工,唉我真的没招