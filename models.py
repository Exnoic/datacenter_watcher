from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import CheckConstraint
from sqlmodel import Field, Relationship, SQLModel


class Server(SQLModel, table=True):
    serial_number: str = Field(primary_key=True)
    name: str = Field(index=True)
    ip: str = Field(index=True)
    model: str
    health: str
    temperatures: list["Temperature"] = Relationship(back_populates="server")
    bios_version: str
    manufacturer: str
    power_state: str
    processor_model: str
    memory: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    rack: int
    position: int
    zone: int = Field(default = 0)


class Sensor(SQLModel, table=True):
    sensor_id: str = Field(primary_key=True)
    ip: str
    location: str
    rack: int
    temperatures: list["Temperature"] = Relationship(back_populates="sensor")
    serial: str
    firmware: str
    position: int
    zone: int = Field(default = 0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)

class Temperature(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint(
            "(server_serial IS NULL) != (sensor_id IS NULL)", name="temperature_one_source",),
    )

    id: int | None = Field(primary_key=True, default=None)
    value: float | None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)

    server_serial: str | None = Field(default=None, foreign_key="server.serial_number", index=True)
    sensor_id: str | None = Field(default=None, foreign_key="sensor.sensor_id", index=True)

    server: Optional["Server"] = Relationship(back_populates="temperatures")
    sensor: Optional["Sensor"] = Relationship(back_populates="temperatures")

class Notification(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    resolved_at: datetime | None = None
