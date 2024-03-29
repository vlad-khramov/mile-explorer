"""empty message

Revision ID: 0003
Revises: 0002
Create Date: 2019-01-12 20:37:39.551656

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('wallets', sa.Column('valid_before_block', sa.BigInteger(), nullable=True))
    op.create_index('transactions__valid_before_block', 'wallets', ['valid_before_block'], unique=False)
    op.drop_index('transactions__update_needed', table_name='wallets')
    op.drop_column('wallets', 'update_needed')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('wallets', sa.Column('update_needed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False))
    op.create_index('transactions__update_needed', 'wallets', ['update_needed'], unique=False)
    op.drop_index('transactions__valid_before_block', table_name='wallets')
    op.drop_column('wallets', 'valid_before_block')
    # ### end Alembic commands ###
