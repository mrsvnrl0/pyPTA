"""Manual holdings read the same applied Parente engine as paper execution."""
from .core import DataError

class PositionNeuralReader:
    def __init__(self, *, shared_engine):
        self.shared_engine=shared_engine

    def read(self,candles,asset,rules,settings_path,now):
        names=[name for name,cfg in self.shared_engine.assets.items() if cfg["symbol"].split("/")[0]==asset]
        if len(names)!=1:raise DataError("Position asset has no unique applied Parente input")
        return self.shared_engine.read(names[0],candles,now)
