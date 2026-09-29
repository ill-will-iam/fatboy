from .acro import Acro

async def setup(bot):
    await bot.add_cog(Acro(bot))
